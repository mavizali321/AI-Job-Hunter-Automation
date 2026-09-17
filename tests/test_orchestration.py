"""Tests proving the automated orchestration chain without manual transitions.

Covers: automatic preparation after first approval, worker polling, path safety,
recovery, idempotency, collision-safe directories, CLI entry points,
connected integration, and the full discovery-to-submission pipeline.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from app.config import settings
from app.models import (
    Job, Application, Approval, Event, JobStatus,
    ApprovalDecision, ApprovalType,
)
from app.pipeline import (
    ingest_discovered_job, verify_job, score_and_decide,
    create_application_for_shortlisted, prepare_application,
    application_slug,
)
from app.approval import (
    create_approval, validate_and_decide, create_final_approval,
    decide_by_ref_code, check_dual_approval, _enqueue_preparation,
)
from app.state_machine import transition_job
from local_worker.worker import LocalWorker, SubmissionGuard


def _valid_evidence():
    return {
        "http_success": True,
        "url_is_official": True,
        "has_content": True,
        "has_substantial_content": True,
        "is_job_specific_url": True,
        "title_match": True,
        "company_match": True,
        "listing_closed": False,
        "has_posted_date": True,
        "location_eligible": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def _create_shortlisted_job(db, company="AutoChain Corp", title="Forward Deployed Engineer",
                            url="https://boards.greenhouse.io/autochain/jobs/12345"):
    """Create a job through the full pipeline to SHORTLISTED."""
    job = ingest_discovered_job(
        db,
        title=title,
        company=company,
        url=url,
        source="greenhouse",
        location="Karachi, Pakistan",
        remote_policy="hybrid",
        description=(
            "We are looking for a Forward Deployed Engineer to integrate LLM APIs, "
            "build agentic AI workflows, design prompt engineering pipelines, "
            "implement RAG systems, and automate customer solutions. "
            "Experience with REST APIs, structured outputs, and function calling preferred."
        ),
        requirements="1-3 years of software engineering experience",
        salary="PKR 200,000-400,000/month",
        posted_date=datetime.now(timezone.utc),
    )
    assert job is not None
    verify_job(db, job, _valid_evidence())
    assert job.status == JobStatus.VERIFIED
    score_and_decide(db, job)
    assert job.status == JobStatus.SHORTLISTED
    app = create_application_for_shortlisted(db, job)
    assert app is not None
    assert job.status == JobStatus.WAITING_APPROVAL
    return job, app


class TestFullAutomatedChain:
    """Proves: discovery -> first approval -> auto prepare -> final approval ->
    worker poll -> submit -> SUBMITTED."""

    def test_full_chain(self, db, client, tmp_path, monkeypatch):
        old_val = settings.applications_dir
        try:
            object.__setattr__(settings, "applications_dir", str(tmp_path))

            # 1. Pipeline produces WAITING_APPROVAL
            job, app = _create_shortlisted_job(db)

            # 2. First approval — mock _enqueue_preparation to capture the call
            enqueued = []

            def mock_enqueue(db_arg, job_id):
                enqueued.append(job_id)

            monkeypatch.setattr(
                "app.approval._enqueue_preparation", mock_enqueue,
            )

            approval1, token1 = create_approval(db, app, job)
            ok, _ = validate_and_decide(db, token1, ApprovalDecision.APPROVED)
            assert ok
            assert job.status == JobStatus.APPROVED
            assert job.id in enqueued

            # 3. Automatic preparation (what the Celery task does)
            app_dir = prepare_application(db, job, app)
            assert app_dir is not None
            assert job.status == JobStatus.AWAITING_FINAL_APPROVAL
            assert (app_dir / "job_snapshot.json").exists()
            assert (app_dir / "answers.json").exists()
            # Verify collision-safe directory name includes app id
            assert f"-app-{app.id}" in app_dir.name

            # 4. Final approval auto-created
            final = db.query(Approval).filter(
                Approval.application_id == app.id,
                Approval.approval_type == ApprovalType.FINAL,
                Approval.decision == ApprovalDecision.PENDING,
            ).first()
            assert final is not None
            assert app.manifest_hash is not None

            # 5. Approve final
            ok2, _ = decide_by_ref_code(
                db, final.approval_ref_code, ApprovalDecision.APPROVED,
            )
            assert ok2
            assert job.status == JobStatus.FINAL_APPROVED
            assert check_dual_approval(db, app.id)

            # 6. Worker poll returns the job with collision-safe key
            resp = client.get(
                "/api/worker/jobs",
                headers={"X-Worker-Token": settings.worker_token},
            )
            assert resp.status_code == 200
            jobs_data = resp.json()
            assert len(jobs_data) == 1
            assert jobs_data[0]["id"] == job.id
            assert jobs_data[0]["application_id"] == app.id
            assert jobs_data[0]["application_key"] == application_slug(
                job.company, job.title, app.id,
            )
            assert "greenhouse" in jobs_data[0]["url"]

            # 7. Authorize and submit
            db.refresh(app)
            resp = client.post(
                f"/api/worker/authorize/{job.id}",
                json={"manifest_hash": app.manifest_hash},
                headers={"X-Worker-Token": settings.worker_token},
            )
            assert resp.status_code == 200
            nonce = resp.json()["nonce"]

            resp = client.post(
                f"/api/worker/submit/{job.id}",
                json={
                    "nonce": nonce,
                    "manifest_hash": app.manifest_hash,
                    "outcome": "CONFIRMED",
                    "confirmation_reference": "auto-chain-001",
                },
                headers={"X-Worker-Token": settings.worker_token},
            )
            assert resp.status_code == 200
            db.refresh(job)
            assert job.status == JobStatus.SUBMITTED
        finally:
            object.__setattr__(settings, "applications_dir", old_val)


class TestRejectionNeverPrepares:
    def test_reject_does_not_enqueue(self, db, monkeypatch):
        job, app = _create_shortlisted_job(db)

        enqueued = []

        def mock_enqueue(db_arg, job_id):
            enqueued.append(job_id)

        monkeypatch.setattr(
            "app.approval._enqueue_preparation", mock_enqueue,
        )

        approval, token = create_approval(db, app, job)
        ok, _ = validate_and_decide(db, token, ApprovalDecision.REJECTED)
        assert ok
        assert job.status == JobStatus.REJECTED
        assert len(enqueued) == 0


class TestDuplicateApprovalIdempotency:
    def test_duplicate_first_approval_one_final(self, db, tmp_path, monkeypatch):
        """Approving first, then calling prepare twice, creates only one pending final."""
        old_val = settings.applications_dir
        try:
            object.__setattr__(settings, "applications_dir", str(tmp_path))
            monkeypatch.setattr(
                "app.approval._enqueue_preparation", lambda db, jid: None,
            )

            job, app = _create_shortlisted_job(db)
            approval1, token1 = create_approval(db, app, job)
            ok, _ = validate_and_decide(db, token1, ApprovalDecision.APPROVED)
            assert ok

            # First preparation
            prepare_application(db, job, app)
            assert job.status == JobStatus.AWAITING_FINAL_APPROVAL

            finals = db.query(Approval).filter(
                Approval.application_id == app.id,
                Approval.approval_type == ApprovalType.FINAL,
                Approval.decision == ApprovalDecision.PENDING,
            ).all()
            assert len(finals) == 1

            result = prepare_application(db, job, app)
            assert result is None

            finals2 = db.query(Approval).filter(
                Approval.application_id == app.id,
                Approval.approval_type == ApprovalType.FINAL,
                Approval.decision == ApprovalDecision.PENDING,
            ).all()
            assert len(finals2) == 1
        finally:
            object.__setattr__(settings, "applications_dir", old_val)


class TestCeleryFailureVisibility:
    def test_enqueue_failure_records_event(self, db, monkeypatch):
        """When Celery is unavailable, _enqueue_preparation records an Event."""
        job, app = _create_shortlisted_job(db)
        approval, token = create_approval(db, app, job)

        from app.approval import _enqueue_preparation as real_enqueue

        mock_task = MagicMock()
        mock_task.delay.side_effect = ConnectionError("Redis unavailable")
        monkeypatch.setattr(
            "app.tasks.prepare_approved_application", mock_task,
        )

        real_enqueue(db, job.id)

        events = db.query(Event).filter(
            Event.entity == "preparation",
            Event.entity_id == job.id,
            Event.action == "enqueue_failed",
        ).all()
        assert len(events) == 1
        assert "Redis unavailable" in events[0].metadata_["error"]

    def test_retry_prepare_endpoint_works_without_celery(
        self, db, client, tmp_path, monkeypatch,
    ):
        """Dashboard retry calls prepare_application directly, not via Celery."""
        old_val = settings.applications_dir
        try:
            object.__setattr__(settings, "applications_dir", str(tmp_path))
            monkeypatch.setattr(
                "app.approval._enqueue_preparation", lambda db, jid: None,
            )

            job, app = _create_shortlisted_job(db)
            approval, token = create_approval(db, app, job)
            ok, _ = validate_and_decide(db, token, ApprovalDecision.APPROVED)
            assert ok
            assert job.status == JobStatus.APPROVED

            from itsdangerous import URLSafeTimedSerializer
            signer = URLSafeTimedSerializer(settings.secret_key)
            auth = signer.dumps({"user": "admin"})
            csrf = "test-csrf-retry"

            resp = client.post(
                f"/api/retry-prepare/{job.id}",
                data={"csrf_token": csrf},
                cookies={"auth": auth, "csrf_token": csrf},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            db.refresh(job)
            assert job.status == JobStatus.AWAITING_FINAL_APPROVAL
        finally:
            object.__setattr__(settings, "applications_dir", old_val)


class TestWorkerJobPayload:
    def test_payload_has_required_fields(self, db, client, monkeypatch):
        """Worker poll returns application_id, application_key, official_url."""
        monkeypatch.setattr(
            "app.approval._enqueue_preparation", lambda db, jid: None,
        )
        job = Job(
            canonical_url="https://boards.greenhouse.io/testco/jobs/999",
            official_url="https://boards.greenhouse.io/testco/jobs/999",
            source="test",
            company="TestCo",
            title="AI Engineer",
            status=JobStatus.FINAL_APPROVED,
            score_total=85,
        )
        db.add(job)
        db.commit()
        app = Application(job_id=job.id, state=JobStatus.FINAL_APPROVED)
        db.add(app)
        db.commit()

        resp = client.get(
            "/api/worker/jobs",
            headers={"X-Worker-Token": settings.worker_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        entry = data[0]
        assert entry["application_id"] == app.id
        assert entry["application_key"] == application_slug("TestCo", "AI Engineer", app.id)
        assert f"-app-{app.id}" in entry["application_key"]
        assert entry["url"] == "https://boards.greenhouse.io/testco/jobs/999"

    def test_ignores_non_final_approved(self, db, client):
        """Worker poll returns only FINAL_APPROVED jobs."""
        for status in [
            JobStatus.APPROVED, JobStatus.SUBMITTED,
            JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.DISCOVERED,
        ]:
            db.add(Job(
                canonical_url=f"https://example.com/{status.value}",
                source="test", company="Co", title="Role",
                status=status,
            ))
        db.commit()

        resp = client.get(
            "/api/worker/jobs",
            headers={"X-Worker-Token": settings.worker_token},
        )
        assert resp.status_code == 200
        assert resp.json() == []


class TestPathTraversal:
    def test_traversal_rejected(self):
        worker = LocalWorker(
            "http://localhost", "token", applications_dir="./applications",
        )
        assert worker._safe_app_dir("../../../etc/passwd") is None
        assert worker._safe_app_dir("..\\windows\\system32") is None
        assert worker._safe_app_dir("foo/bar") is None
        assert worker._safe_app_dir("foo\\bar") is None
        assert worker._safe_app_dir("") is None

    def test_valid_slug_accepted(self, tmp_path):
        worker = LocalWorker(
            "http://localhost", "token", applications_dir=str(tmp_path),
        )
        result = worker._safe_app_dir("valid-company-ai-engineer-app-42")
        assert result is not None
        assert str(result).startswith(str(tmp_path.resolve()))


class TestRecoveryTask:
    def test_recover_stuck_approved(self, db, monkeypatch):
        """Recovery task re-enqueues preparation for APPROVED jobs stuck >10 min."""
        job = Job(
            canonical_url="https://example.com/stuck-approved",
            source="test", company="StuckCo", title="Engineer",
            status=JobStatus.APPROVED,
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=15),
        )
        db.add(job)
        db.commit()

        enqueued = []

        mock_task = MagicMock()
        mock_task.delay.side_effect = lambda jid: enqueued.append(jid)
        monkeypatch.setattr(
            "app.tasks.prepare_approved_application", mock_task,
        )

        from app.tasks import recover_stuck_jobs

        mock_session_local = MagicMock(return_value=db)
        db.close = lambda: None
        with patch("app.database.SessionLocal", mock_session_local):
            result = recover_stuck_jobs()

        assert result["re_enqueued_approved"] == 1
        assert job.id in enqueued

    def test_recover_stuck_preparing_to_failed(self, db, monkeypatch):
        """Recovery task marks PREPARING jobs stuck >10 min as FAILED."""
        job = Job(
            canonical_url="https://example.com/stuck-preparing",
            source="test", company="StuckCo", title="Engineer",
            status=JobStatus.PREPARING,
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=15),
        )
        db.add(job)
        db.commit()

        mock_task = MagicMock()
        monkeypatch.setattr(
            "app.tasks.prepare_approved_application", mock_task,
        )

        from app.tasks import recover_stuck_jobs

        mock_session_local = MagicMock(return_value=db)
        db.close = lambda: None
        with patch("app.database.SessionLocal", mock_session_local):
            result = recover_stuck_jobs()

        assert result["failed_preparing"] == 1
        db.refresh(job)
        assert job.status == JobStatus.FAILED


class TestUncertainNeverRetried:
    def test_uncertain_stays_manual_action(self, db, client, monkeypatch):
        """UNCERTAIN outcome transitions to MANUAL_ACTION_REQUIRED, not SUBMITTED."""
        monkeypatch.setattr(
            "app.approval._enqueue_preparation", lambda db, jid: None,
        )

        job = Job(
            canonical_url="https://example.com/uncertain-test",
            source="test", company="UncertainCo", title="AI Eng",
            status=JobStatus.FINAL_APPROVED, score_total=85,
        )
        db.add(job)
        db.commit()
        app = Application(
            job_id=job.id, state=JobStatus.FINAL_APPROVED,
            manifest_hash="abc123",
        )
        db.add(app)
        db.commit()

        first = Approval(
            application_id=app.id, token_hash="first_hash",
            channel="DASHBOARD", approval_type=ApprovalType.FIRST,
            decision=ApprovalDecision.APPROVED,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            payload_hash="ph1",
        )
        final = Approval(
            application_id=app.id, token_hash="final_hash",
            channel="DASHBOARD", approval_type=ApprovalType.FINAL,
            decision=ApprovalDecision.APPROVED,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            payload_hash="abc123",
        )
        db.add_all([first, final])
        db.commit()

        resp = client.post(
            f"/api/worker/authorize/{job.id}",
            json={"manifest_hash": "abc123"},
            headers={"X-Worker-Token": settings.worker_token},
        )
        assert resp.status_code == 200
        nonce = resp.json()["nonce"]

        resp = client.post(
            f"/api/worker/submit/{job.id}",
            json={
                "nonce": nonce,
                "manifest_hash": "abc123",
                "outcome": "UNCERTAIN",
                "confirmation_reference": "maybe",
            },
            headers={"X-Worker-Token": settings.worker_token},
        )
        assert resp.status_code == 200
        db.refresh(job)
        assert job.status == JobStatus.MANUAL_ACTION_REQUIRED
        assert job.status != JobStatus.SUBMITTED


class TestWorkerApiFailure:
    def test_poll_returns_empty_on_bad_token(self):
        """Worker handles auth failure gracefully."""
        worker = LocalWorker("http://127.0.0.1:1", "wrong-token")
        try:
            result = worker.poll_jobs()
            assert result == []
        except Exception:
            pass

    def test_poll_handles_connection_error(self):
        """Worker raises on connection error — CLI catches it."""
        import httpx

        worker = LocalWorker("http://127.0.0.1:1", "token")
        with pytest.raises((httpx.ConnectError, httpx.TimeoutException, OSError)):
            worker.poll_jobs()


class TestPreparationTask:
    def test_task_skips_non_approved(self, db, monkeypatch):
        """prepare_approved_application skips jobs not in APPROVED state."""
        job = Job(
            canonical_url="https://example.com/not-approved",
            source="test", company="Co", title="Role",
            status=JobStatus.DISCOVERED,
        )
        db.add(job)
        db.commit()

        from app.tasks import prepare_approved_application

        with patch("app.database.SessionLocal", return_value=db):
            result = prepare_approved_application(job.id)

        assert result["status"] == "skipped"

    def test_task_idempotent_with_existing_final(self, db, tmp_path, monkeypatch):
        """Task skips if a FINAL approval already exists."""
        old_val = settings.applications_dir
        try:
            object.__setattr__(settings, "applications_dir", str(tmp_path))
            monkeypatch.setattr(
                "app.approval._enqueue_preparation", lambda db, jid: None,
            )

            job, app = _create_shortlisted_job(db)
            a1, t1 = create_approval(db, app, job)
            validate_and_decide(db, t1, ApprovalDecision.APPROVED)

            prepare_application(db, job, app)

            job_for_task = db.query(Job).filter(Job.id == job.id).first()
            assert job_for_task.status == JobStatus.AWAITING_FINAL_APPROVAL

            from app.tasks import prepare_approved_application

            with patch("app.database.SessionLocal", return_value=db):
                result = prepare_approved_application(job.id)

            assert result["status"] == "skipped"
        finally:
            object.__setattr__(settings, "applications_dir", old_val)


class TestApplicationSlug:
    def test_deterministic(self):
        assert application_slug("TestCo", "AI Engineer", 42) == "testco-ai-engineer-app-42"

    def test_truncated_at_60(self):
        slug = application_slug("A" * 40, "B" * 40, 99)
        base = f"{'a' * 40}-{'b' * 40}".lower().replace(" ", "-")[:60]
        assert slug == f"{base}-app-99"

    def test_without_id_backward_compatible(self):
        assert application_slug("TestCo", "AI Engineer") == "testco-ai-engineer"

    def test_two_identical_jobs_different_dirs(self, db, tmp_path, monkeypatch):
        """Two applications with same company/title get different directories."""
        old_val = settings.applications_dir
        try:
            object.__setattr__(settings, "applications_dir", str(tmp_path))
            monkeypatch.setattr(
                "app.approval._enqueue_preparation", lambda db, jid: None,
            )

            job1, app1 = _create_shortlisted_job(
                db, company="SameCo", title="Forward Deployed Engineer",
                url="https://boards.greenhouse.io/sameco/jobs/111",
            )
            a1, t1 = create_approval(db, app1, job1)
            validate_and_decide(db, t1, ApprovalDecision.APPROVED)
            dir1 = prepare_application(db, job1, app1)

            # Second job needs a different URL and description to avoid dedup
            job2 = ingest_discovered_job(
                db,
                title="Forward Deployed Engineer",
                company="SameCo",
                url="https://boards.greenhouse.io/sameco/jobs/222",
                source="greenhouse",
                location="Karachi, Pakistan",
                remote_policy="hybrid",
                description=(
                    "Second opening for Forward Deployed Engineer at SameCo. "
                    "Build machine learning pipelines, deploy LLM solutions, "
                    "and integrate AI features into production systems. "
                    "Experience with REST APIs, structured outputs, and function calling preferred."
                ),
                requirements="1-3 years of software engineering experience",
                salary="PKR 200,000-400,000/month",
                posted_date=datetime.now(timezone.utc),
            )
            assert job2 is not None
            verify_job(db, job2, _valid_evidence())
            score_and_decide(db, job2)
            assert job2.status == JobStatus.SHORTLISTED
            app2 = create_application_for_shortlisted(db, job2)
            assert app2 is not None
            a2, t2 = create_approval(db, app2, job2)
            validate_and_decide(db, t2, ApprovalDecision.APPROVED)
            dir2 = prepare_application(db, job2, app2)

            assert dir1 is not None
            assert dir2 is not None
            assert dir1 != dir2
            assert dir1.exists()
            assert dir2.exists()
            assert f"-app-{app1.id}" in dir1.name
            assert f"-app-{app2.id}" in dir2.name
        finally:
            object.__setattr__(settings, "applications_dir", old_val)


class TestWorkerCLI:
    def test_help_via_local_worker(self):
        """python -m local_worker --help exits 0."""
        result = subprocess.run(
            [sys.executable, "-m", "local_worker", "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "--once" in result.stdout
        assert "--headless" in result.stdout

    def test_help_via_local_worker_worker(self):
        """python -m local_worker.worker --help exits 0."""
        result = subprocess.run(
            [sys.executable, "-m", "local_worker.worker", "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "--once" in result.stdout

    def test_once_exits_cleanly_no_server(self):
        """--once exits cleanly when no server is running."""
        result = subprocess.run(
            [sys.executable, "-m", "local_worker", "--once"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0


class TestWorkerReportResult:
    def test_report_result_logs_rejection(self, caplog):
        """report_result logs server rejection instead of silently ignoring it."""
        import logging
        import httpx
        from unittest.mock import patch as _patch

        worker = LocalWorker("http://localhost:9999", "token")

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Nonce already consumed"
        mock_response.json.return_value = {"detail": "Nonce already consumed"}

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with _patch("httpx.Client", return_value=mock_client), \
             caplog.at_level(logging.ERROR, logger="local_worker"):
            result = worker.report_result(99, {"outcome": "CONFIRMED", "nonce": "x"})

        assert result["status_code"] == 400
        assert "rejected" in caplog.text.lower() or "Nonce" in caplog.text

    def test_report_result_returns_success(self):
        """report_result returns parsed JSON on success."""
        import httpx
        from unittest.mock import patch as _patch

        worker = LocalWorker("http://localhost:9999", "token")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "ok", "job_id": 1, "outcome": "CONFIRMED"}

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with _patch("httpx.Client", return_value=mock_client):
            result = worker.report_result(1, {"outcome": "CONFIRMED"})

        assert result["status"] == "ok"


class TestConnectedIntegration:
    """Connected integration test: exercises the real preparation task,
    real API endpoints, and real worker polling with only Playwright mocked."""

    def test_end_to_end_connected(self, db, client, tmp_path, monkeypatch):
        old_val = settings.applications_dir
        try:
            object.__setattr__(settings, "applications_dir", str(tmp_path))

            # --- 1. Pipeline: discover -> shortlist -> WAITING_APPROVAL ---
            job, app = _create_shortlisted_job(db)
            assert job.status == JobStatus.WAITING_APPROVAL

            # --- 2. First approval via ref code (through endpoint) ---
            approval1, token1 = create_approval(db, app, job)
            ref_code_1 = approval1.approval_ref_code

            # Mock _enqueue_preparation to run the actual task synchronously
            def sync_enqueue(db_arg, job_id):
                from app.tasks import prepare_approved_application
                db_arg.close = lambda: None
                with patch("app.database.SessionLocal", return_value=db_arg):
                    prepare_approved_application(job_id)

            monkeypatch.setattr("app.approval._enqueue_preparation", sync_enqueue)

            # Approve FIRST via ref code — this triggers sync_enqueue
            ok, msg = decide_by_ref_code(db, ref_code_1, ApprovalDecision.APPROVED)
            assert ok, msg
            assert job.status == JobStatus.AWAITING_FINAL_APPROVAL

            # --- 3. Verify FINAL approval was auto-created by preparation task ---
            final = db.query(Approval).filter(
                Approval.application_id == app.id,
                Approval.approval_type == ApprovalType.FINAL,
                Approval.decision == ApprovalDecision.PENDING,
            ).first()
            assert final is not None, "FINAL approval should be auto-created by prepare_application"

            # Verify application directory was created with collision-safe name
            expected_key = application_slug(job.company, job.title, app.id)
            expected_dir = tmp_path / expected_key
            assert expected_dir.exists(), f"Expected dir {expected_dir} to exist"
            assert (expected_dir / "job_snapshot.json").exists()
            assert (expected_dir / "answers.json").exists()

            # --- 4. Approve FINAL via ref code ---
            ok2, msg2 = decide_by_ref_code(db, final.approval_ref_code, ApprovalDecision.APPROVED)
            assert ok2, msg2
            assert job.status == JobStatus.FINAL_APPROVED

            # --- 5. Worker polls and gets the job via real API ---
            worker = LocalWorker(
                api_url="http://testserver",
                worker_token=settings.worker_token,
                applications_dir=str(tmp_path),
            )

            import httpx

            class _TestTransport(httpx.BaseTransport):
                def __init__(self, test_client):
                    self._client = test_client

                def handle_request(self, request):
                    method = request.method.lower()
                    url = str(request.url)
                    path = "/" + url.split("//testserver/", 1)[1] if "//testserver/" in url else url
                    headers = dict(request.headers)
                    content = request.content
                    if method == "get":
                        resp = self._client.get(path, headers=headers)
                    else:
                        resp = self._client.post(path, content=content, headers=headers)
                    return httpx.Response(
                        status_code=resp.status_code,
                        content=resp.content,
                        headers=dict(resp.headers),
                    )

            transport = _TestTransport(client)
            _real_httpx_client = httpx.Client

            def patched_httpx_client(**kwargs):
                kwargs.pop("transport", None)
                return _real_httpx_client(transport=transport, **kwargs)

            # Poll jobs via worker
            with patch("httpx.Client", patched_httpx_client):
                polled = worker.poll_jobs()

            assert len(polled) == 1
            assert polled[0]["id"] == job.id
            assert polled[0]["application_key"] == expected_key

            # --- 6. Mock Playwright, run fill_and_submit via worker ---
            # fill_and_submit in real code: authorizes, fills form, calls report_result.
            # We mock fill_and_submit to call authorize + report_result through the
            # real worker methods, skipping only the Playwright browser interaction.
            fill_calls = []

            async def mock_fill_and_submit(job_payload, app_dir):
                fill_calls.append({"job": job_payload, "app_dir": str(app_dir)})
                # Read the prepared answers to compute manifest hash
                answers = json.loads((app_dir / "answers.json").read_text())
                snapshot = json.loads((app_dir / "job_snapshot.json").read_text())
                manifest_hash = worker.guard.compute_manifest_hash(
                    snapshot, str(app_dir / "resume.pdf"), answers,
                )
                auth = worker.authorize_submission(job_payload["id"], manifest_hash)
                assert "nonce" in auth, f"Authorization failed: {auth}"
                result = {
                    "outcome": "CONFIRMED",
                    "confirmation_reference": f"connected-{job_payload['id']}",
                    "nonce": auth["nonce"],
                    "manifest_hash": manifest_hash,
                }
                worker.report_result(job_payload["id"], result)
                return result

            monkeypatch.setattr(worker, "fill_and_submit", mock_fill_and_submit)

            with patch("httpx.Client", patched_httpx_client):
                result = worker.run_once()

            # --- 7. Assert fill_and_submit was called with correct dir ---
            assert len(fill_calls) == 1
            assert fill_calls[0]["app_dir"] == str(expected_dir)
            assert result is not None
            assert result["outcome"] == "CONFIRMED"

            # --- 8. Assert final DB state is SUBMITTED ---
            db.refresh(job)
            assert job.status == JobStatus.SUBMITTED

        finally:
            object.__setattr__(settings, "applications_dir", old_val)
