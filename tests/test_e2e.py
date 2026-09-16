"""End-to-end dry run test: discover -> verify -> score -> shortlist -> approve -> prepare -> final approve -> submit guard."""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from app.models import Job, Application, Approval, Event, JobStatus, ApprovalDecision, ApprovalChannel
from app.pipeline import ingest_discovered_job, verify_job, score_and_decide, create_application_for_shortlisted
from app.approval import (
    create_approval, validate_and_decide, create_final_approval,
    check_dual_approval, decide_by_ref_code,
)
from app.state_machine import transition_job, InvalidTransitionError
from local_worker.worker import SubmissionGuard


def _valid_evidence():
    return {
        "http_success": True,
        "url_is_official": True,
        "has_content": True,
        "listing_closed": False,
        "has_posted_date": True,
        "location_eligible": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


class TestE2EDryRun:
    def test_full_flow(self, db):
        job = ingest_discovered_job(
            db,
            title="Forward Deployed Engineer",
            company="AI Startup",
            url="https://aistartup.com/careers/fde-001",
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
            external_id="fde-001",
        )
        assert job is not None
        assert job.status == JobStatus.DISCOVERED

        assert verify_job(db, job, _valid_evidence())
        assert job.status == JobStatus.VERIFIED

        result = score_and_decide(db, job)
        assert result["total"] >= 70
        assert job.score_total >= 70

        if job.status == JobStatus.SHORTLISTED:
            app = create_application_for_shortlisted(db, job)
            assert app is not None
            assert job.status == JobStatus.WAITING_APPROVAL

            approval1, token1 = create_approval(db, app, job)
            assert approval1.approval_ref_code is not None
            ok, msg = validate_and_decide(db, token1, ApprovalDecision.APPROVED)
            assert ok
            assert job.status == JobStatus.APPROVED

            transition_job(db, job, JobStatus.PREPARING)
            app.state = JobStatus.PREPARING
            db.commit()

            transition_job(db, job, JobStatus.READY_TO_SUBMIT)
            app.state = JobStatus.READY_TO_SUBMIT
            db.commit()

            guard = SubmissionGuard()
            snapshot = {
                "job_id": job.id,
                "company": job.company,
                "title": job.title,
                "url": job.canonical_url,
            }
            answers = {"full_name": "Maviz Ali", "email": "maviz.ali92@gmail.com"}
            manifest_hash = guard.compute_manifest_hash(snapshot, "profile/Maviz-Ali-Resume-Original.pdf", answers)

            transition_job(db, job, JobStatus.AWAITING_FINAL_APPROVAL)
            app.state = JobStatus.AWAITING_FINAL_APPROVAL
            db.commit()

            approval2, token2 = create_final_approval(db, app, job, manifest_hash)
            assert approval2.payload_hash == manifest_hash
            assert approval2.approval_ref_code is not None
            assert app.manifest_hash == manifest_hash

            current_hash = guard.compute_manifest_hash(snapshot, "profile/Maviz-Ali-Resume-Original.pdf", answers)
            assert guard.verify_manifest(current_hash, manifest_hash)

            ok2, msg2 = validate_and_decide(db, token2, ApprovalDecision.APPROVED)
            assert ok2
            assert job.status == JobStatus.FINAL_APPROVED

            assert check_dual_approval(db, app.id)

            transition_job(db, job, JobStatus.SUBMITTED)
            app.state = JobStatus.SUBMITTED
            app.submitted_at = datetime.now(timezone.utc)
            app.confirmation = "Submitted via dry-run test"
            db.commit()

            assert job.status == JobStatus.SUBMITTED
            assert app.confirmation is not None

        dup = ingest_discovered_job(
            db,
            title="Forward Deployed Engineer",
            company="AI Startup",
            url="https://aistartup.com/careers/fde-001",
            source="greenhouse",
        )
        assert dup is None
        assert db.query(Job).filter(Job.company == "AI Startup").count() == 1

    def test_cannot_submit_from_ready_to_submit(self, db):
        """READY_TO_SUBMIT -> SUBMITTED is no longer valid."""
        job = Job(
            canonical_url="https://example.com/no-direct-submit",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.READY_TO_SUBMIT,
        )
        db.add(job)
        db.commit()
        import pytest
        with pytest.raises(InvalidTransitionError):
            transition_job(db, job, JobStatus.SUBMITTED)

    def test_no_submit_without_two_approvals(self, db):
        job = ingest_discovered_job(
            db, "AI Engineer", "NeedApprovalCo",
            "https://needapproval.com/job1", "test",
            location="Karachi", description="LLM API integration, agentic AI, prompt engineering, RAG, automation",
        )
        verify_job(db, job, _valid_evidence())
        score_and_decide(db, job)

        if job.status == JobStatus.SHORTLISTED:
            app = create_application_for_shortlisted(db, job)
            assert not check_dual_approval(db, app.id)

    def test_manual_action_for_sensitive(self, db):
        from app.scoring import SENSITIVE_FIELDS
        assert len(SENSITIVE_FIELDS) > 0
        assert "work authorization" in SENSITIVE_FIELDS
        assert "visa" in SENSITIVE_FIELDS

    def test_resume_checksum_unchanged(self):
        resume_path = Path("profile/Maviz-Ali-Resume-Original.pdf")
        if not resume_path.exists():
            return
        h = hashlib.sha256()
        with open(resume_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        assert h.hexdigest().upper() == "E47DD0E6E27E3FE906630219A1CA44D450BE5DC2F07186FF8637D17CCE59DD4E"


class TestRefCodeApprovals:
    def test_approve_by_ref_code(self, db):
        job = ingest_discovered_job(
            db, "AI Engineer", "RefCo",
            "https://refco.com/job", "test",
            location="Karachi", description="LLM API integration, agentic AI, prompt engineering, RAG",
        )
        verify_job(db, job, _valid_evidence())
        score_and_decide(db, job)
        if job.status != JobStatus.SHORTLISTED:
            return
        app = create_application_for_shortlisted(db, job)
        approval, token = create_approval(db, app, job)
        ref = approval.approval_ref_code
        assert ref is not None
        ok, msg = decide_by_ref_code(db, ref, ApprovalDecision.APPROVED)
        assert ok
        assert job.status == JobStatus.APPROVED

    def test_wrong_ref_code_fails(self, db):
        ok, msg = decide_by_ref_code(db, "ZZZZZZZZ", ApprovalDecision.APPROVED)
        assert not ok
        assert "No pending" in msg

    def test_generic_approve_without_ref_is_ambiguous(self):
        from app.whatsapp import WhatsAppClient
        decision, ref_code = WhatsAppClient.parse_approval_response("APPROVE")
        assert decision == "APPROVED"
        assert ref_code is None


class TestPayloadChangeInvalidation:
    def test_payload_change_rejects_approval(self, db):
        job = ingest_discovered_job(
            db, "AI Engineer", "PayloadCo",
            "https://payloadco.com/job", "test",
            location="Karachi", description="LLM API integration, agentic AI, prompt engineering, RAG",
        )
        verify_job(db, job, _valid_evidence())
        score_and_decide(db, job)
        if job.status != JobStatus.SHORTLISTED:
            return
        app = create_application_for_shortlisted(db, job)
        approval, token = create_approval(db, app, job)
        job.score_total = 999
        db.commit()
        ok, msg = validate_and_decide(db, token, ApprovalDecision.APPROVED)
        assert not ok
        assert "Payload has changed" in msg


class TestDualApprovalEnforcement:
    def test_single_approval_insufficient(self, db):
        job = ingest_discovered_job(
            db, "AI Engineer", "DualCo",
            "https://dualco.com/job", "test",
            location="Karachi", description="LLM API integration, agentic AI, prompt engineering, RAG",
        )
        verify_job(db, job, _valid_evidence())
        score_and_decide(db, job)
        if job.status != JobStatus.SHORTLISTED:
            return
        app = create_application_for_shortlisted(db, job)
        create_approval(db, app, job)
        assert not check_dual_approval(db, app.id)

    def test_two_approvals_sufficient(self, db):
        job = ingest_discovered_job(
            db, "AI Engineer", "DualCo2",
            "https://dualco2.com/job", "test",
            location="Karachi", description="LLM API integration, agentic AI, prompt engineering, RAG",
        )
        verify_job(db, job, _valid_evidence())
        score_and_decide(db, job)
        if job.status != JobStatus.SHORTLISTED:
            return
        app = create_application_for_shortlisted(db, job)
        a1, t1 = create_approval(db, app, job)
        ok1, _ = validate_and_decide(db, t1, ApprovalDecision.APPROVED)
        assert ok1

        transition_job(db, job, JobStatus.PREPARING)
        db.commit()
        transition_job(db, job, JobStatus.READY_TO_SUBMIT)
        db.commit()
        transition_job(db, job, JobStatus.AWAITING_FINAL_APPROVAL)
        db.commit()

        guard = SubmissionGuard()
        snapshot = {"job_id": job.id, "company": job.company, "title": job.title, "url": job.canonical_url}
        answers = {"full_name": "Maviz Ali"}
        manifest_hash = guard.compute_manifest_hash(snapshot, "resume.pdf", answers)
        a2, t2 = create_final_approval(db, app, job, manifest_hash)

        ok2, _ = validate_and_decide(db, t2, ApprovalDecision.APPROVED)
        assert ok2
        assert job.status == JobStatus.FINAL_APPROVED

        assert check_dual_approval(db, app.id)


class TestCSRFProtection:
    def _auth_token(self):
        from itsdangerous import URLSafeTimedSerializer
        from app.config import settings
        signer = URLSafeTimedSerializer(settings.secret_key)
        return signer.dumps({"user": "admin"})

    def test_approve_without_csrf_rejected(self, client):
        token = self._auth_token()
        resp = client.post(
            "/api/approve-ref/DEADBEEF",
            data={},
            cookies={"auth": token},
        )
        assert resp.status_code == 403

    def test_approve_with_wrong_csrf_rejected(self, client):
        token = self._auth_token()
        resp = client.post(
            "/api/approve-ref/DEADBEEF",
            data={"csrf_token": "wrong"},
            cookies={"auth": token, "csrf_token": "correct"},
        )
        assert resp.status_code == 403

    def test_approve_with_valid_csrf_passes_validation(self, client):
        token = self._auth_token()
        csrf = "test-csrf-token-value"
        resp = client.post(
            "/api/approve-ref/DEADBEEF",
            data={"csrf_token": csrf},
            cookies={"auth": token, "csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code != 403

    def test_reject_without_csrf_rejected(self, client):
        token = self._auth_token()
        resp = client.post(
            "/api/reject-ref/DEADBEEF",
            data={},
            cookies={"auth": token},
        )
        assert resp.status_code == 403
