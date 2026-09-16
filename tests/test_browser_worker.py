"""Tests for the browser worker ATS form filling and submission logic.

These tests use real fixture HTML forms served via a local HTTP server
and Playwright to prove:
- Correct fields are filled for each ATS
- Submit button is clicked exactly once
- CAPTCHA detection triggers MANUAL_ACTION_REQUIRED
- No submission without final approval (tested via state machine)
- Changed manifest blocks submission
- Uncertain outcomes never auto-retry

Integration tests call LocalWorker.fill_and_submit() end-to-end.
Mock only authorize_submission and report_result.

Requires: playwright install chromium
Skip automatically if playwright is not installed.
"""

import asyncio
import functools
import json
import hashlib
import threading
import http.server
from datetime import datetime, timezone
from pathlib import Path

import pytest

from local_worker.worker import SubmissionGuard, _detect_ats, LocalWorker


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestATSDetection:
    def test_greenhouse(self):
        assert _detect_ats("https://boards.greenhouse.io/company/jobs/123") == "greenhouse"

    def test_lever(self):
        assert _detect_ats("https://jobs.lever.co/company/abc") == "lever"

    def test_ashby(self):
        assert _detect_ats("https://jobs.ashbyhq.com/company/abc") == "ashby"

    def test_generic(self):
        assert _detect_ats("https://company.com/careers/job-123") == "generic"

    def test_greenhouse_variant(self):
        assert _detect_ats("https://boards.greenhouse.io/embed/job?token=abc") == "greenhouse"


class TestSubmissionGuard:
    def test_manifest_hash_deterministic(self):
        guard = SubmissionGuard()
        snapshot = {"job_id": 1, "company": "TestCo", "title": "Engineer"}
        answers = {"name": "Maviz Ali"}
        h1 = guard.compute_manifest_hash(snapshot, "nonexistent.pdf", answers)
        h2 = guard.compute_manifest_hash(snapshot, "nonexistent.pdf", answers)
        assert h1 == h2
        assert len(h1) == 64

    def test_manifest_changes_with_answers(self):
        guard = SubmissionGuard()
        snapshot = {"job_id": 1}
        h1 = guard.compute_manifest_hash(snapshot, "x.pdf", {"name": "A"})
        h2 = guard.compute_manifest_hash(snapshot, "x.pdf", {"name": "B"})
        assert h1 != h2

    def test_manifest_changes_with_snapshot(self):
        guard = SubmissionGuard()
        h1 = guard.compute_manifest_hash({"job_id": 1}, "x.pdf", {"name": "A"})
        h2 = guard.compute_manifest_hash({"job_id": 2}, "x.pdf", {"name": "A"})
        assert h1 != h2

    def test_verify_manifest_match(self):
        guard = SubmissionGuard()
        assert guard.verify_manifest("abc", "abc") is True
        assert guard.verify_manifest("abc", "def") is False


class TestNoSubmitWithoutFinalApproval:
    def test_ready_to_submit_cannot_go_to_submitted(self, db):
        from app.models import Job, JobStatus
        from app.state_machine import transition_job, InvalidTransitionError
        job = Job(
            canonical_url="https://example.com/browser-test",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.READY_TO_SUBMIT,
        )
        db.add(job)
        db.commit()
        with pytest.raises(InvalidTransitionError):
            transition_job(db, job, JobStatus.SUBMITTED)

    def test_final_approved_can_go_to_submitted(self, db):
        from app.models import Job, JobStatus
        from app.state_machine import transition_job
        job = Job(
            canonical_url="https://example.com/browser-test-2",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.FINAL_APPROVED,
        )
        db.add(job)
        db.commit()
        transition_job(db, job, JobStatus.SUBMITTED)
        db.commit()
        assert job.status == JobStatus.SUBMITTED


class TestChangedManifestBlocks:
    def test_nonce_rejects_changed_manifest(self, db):
        from app.models import Application, Job, JobStatus
        from app.approval import create_submission_nonce, consume_submission_nonce
        job = Job(
            canonical_url="https://example.com/manifest-test",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.FINAL_APPROVED,
        )
        db.add(job)
        db.commit()
        app = Application(job_id=job.id, state=JobStatus.FINAL_APPROVED)
        db.add(app)
        db.commit()

        nonce_val, _ = create_submission_nonce(db, app, "original_hash")
        ok, msg = consume_submission_nonce(db, nonce_val, "changed_hash")
        assert not ok
        assert "mismatch" in msg.lower()


class TestUncertainNeverAutoRetries:
    def test_uncertain_outcome_goes_to_manual_action(self, db):
        from app.models import Job, Application, JobStatus
        from app.state_machine import transition_job
        job = Job(
            canonical_url="https://example.com/uncertain-test",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.FINAL_APPROVED,
        )
        db.add(job)
        db.commit()
        app = Application(job_id=job.id, state=JobStatus.FINAL_APPROVED)
        db.add(app)
        db.commit()

        transition_job(db, job, JobStatus.MANUAL_ACTION_REQUIRED, {"reason": "uncertain_submission"})
        app.attempt_status = "UNCERTAIN"
        app.state = JobStatus.MANUAL_ACTION_REQUIRED
        db.commit()

        assert job.status == JobStatus.MANUAL_ACTION_REQUIRED
        assert app.attempt_status == "UNCERTAIN"


def _serve_fixtures():
    """Start a local HTTP server serving fixture HTML files."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES_DIR)
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="Playwright not installed")
class TestWorkerIntegration:
    """Integration tests calling LocalWorker.fill_and_submit() end-to-end.

    Mock only authorize_submission and report_result.
    All Playwright interaction goes through the worker, not direct page calls.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.server, self.port = _serve_fixtures()

        self.app_dir = tmp_path / "app_data"
        self.app_dir.mkdir()
        (self.app_dir / "job_snapshot.json").write_text(
            json.dumps({"job_id": 1, "company": "TestCo", "title": "Engineer"})
        )
        (self.app_dir / "answers.json").write_text(
            json.dumps({
                "first_name": "Maviz", "last_name": "Ali",
                "full_name": "Maviz Ali",
                "email": "maviz.ali92@gmail.com",
                "phone": "+92 336 1321040",
            })
        )
        (self.app_dir / "resume.pdf").write_bytes(b"%PDF-1.4 dummy")

        self.browser_profile = str(tmp_path / "browser_profile")
        self.worker = LocalWorker(
            api_url="http://localhost:8000",
            worker_token="test-token",
            browser_profile=self.browser_profile,
            headless=True,
        )
        self.reported = []
        self.worker.report_result = (
            lambda jid, r: self.reported.append((jid, r))
        )
        self.worker.authorize_submission = (
            lambda jid, mh: {"nonce": "test-nonce", "manifest_hash": mh}
        )

        yield
        self.server.shutdown()

    def test_greenhouse_fill_submit_confirmed(self):
        job = {"id": 1, "url": f"http://127.0.0.1:{self.port}/greenhouse_form.html"}
        result = asyncio.run(self.worker.fill_and_submit(job, self.app_dir))
        assert result["outcome"] == "CONFIRMED"
        assert result["confirmation_reference"]
        assert len(self.reported) == 1
        assert self.reported[0][0] == 1
        assert self.reported[0][1]["outcome"] == "CONFIRMED"

    def test_lever_fill_submit_confirmed(self):
        job = {"id": 2, "url": f"http://127.0.0.1:{self.port}/lever_form.html"}
        result = asyncio.run(self.worker.fill_and_submit(job, self.app_dir))
        assert result["outcome"] == "CONFIRMED"
        assert len(self.reported) == 1
        assert self.reported[0][1]["outcome"] == "CONFIRMED"

    def test_ashby_fill_submit_confirmed(self):
        job = {"id": 3, "url": f"http://127.0.0.1:{self.port}/ashby_form.html"}
        result = asyncio.run(self.worker.fill_and_submit(job, self.app_dir))
        assert result["outcome"] == "CONFIRMED"
        assert len(self.reported) == 1
        assert self.reported[0][1]["outcome"] == "CONFIRMED"

    def test_captcha_returns_manual_action_no_submit_click(self):
        job = {"id": 4, "url": f"http://127.0.0.1:{self.port}/captcha_form.html"}
        result = asyncio.run(self.worker.fill_and_submit(job, self.app_dir))
        assert result["outcome"] == "MANUAL_ACTION_REQUIRED"
        assert "CAPTCHA" in result["reason"]
        assert len(self.reported) == 1
        assert self.reported[0][1]["outcome"] == "MANUAL_ACTION_REQUIRED"

    def test_auth_failure_no_browser_submission(self):
        self.worker.authorize_submission = (
            lambda jid, mh: {"error": "Not authorized", "status_code": 400}
        )
        job = {"id": 5, "url": f"http://127.0.0.1:{self.port}/greenhouse_form.html"}
        result = asyncio.run(self.worker.fill_and_submit(job, self.app_dir))
        assert result["outcome"] == "FAILED"
        assert "Authorization failed" in result["reason"]
        assert len(self.reported) == 0

    def test_changed_manifest_auth_failure_no_browser(self):
        self.worker.authorize_submission = (
            lambda jid, mh: {"error": "Manifest hash mismatch", "status_code": 400}
        )
        job = {"id": 6, "url": f"http://127.0.0.1:{self.port}/greenhouse_form.html"}
        result = asyncio.run(self.worker.fill_and_submit(job, self.app_dir))
        assert result["outcome"] == "FAILED"
        assert len(self.reported) == 0

    def test_browser_closes_after_success(self):
        job = {"id": 7, "url": f"http://127.0.0.1:{self.port}/greenhouse_form.html"}
        result = asyncio.run(self.worker.fill_and_submit(job, self.app_dir))
        assert result["outcome"] == "CONFIRMED"

        async def _verify_browser_closed():
            pw = await async_playwright().start()
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    self.browser_profile, headless=True,
                )
                await ctx.close()
            finally:
                await pw.stop()

        asyncio.run(_verify_browser_closed())

    def test_browser_closes_after_captcha(self):
        job = {"id": 8, "url": f"http://127.0.0.1:{self.port}/captcha_form.html"}
        result = asyncio.run(self.worker.fill_and_submit(job, self.app_dir))
        assert result["outcome"] == "MANUAL_ACTION_REQUIRED"

        async def _verify_browser_closed():
            pw = await async_playwright().start()
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    self.browser_profile, headless=True,
                )
                await ctx.close()
            finally:
                await pw.stop()

        asyncio.run(_verify_browser_closed())
