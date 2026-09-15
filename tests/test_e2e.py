"""End-to-end dry run test: discover -> verify -> score -> shortlist -> approve -> prepare -> submit guard."""

import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone

from app.models import Job, Application, Approval, Event, JobStatus, ApprovalDecision, ApprovalChannel
from app.pipeline import ingest_discovered_job, verify_job, score_and_decide, create_application_for_shortlisted
from app.approval import create_approval, validate_and_decide, create_final_approval, check_dual_approval
from app.state_machine import transition_job, InvalidTransitionError
from local_worker.worker import SubmissionGuard


class TestE2EDryRun:
    def test_full_flow(self, db):
        # 1. Discover
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

        # 2. Verify
        assert verify_job(db, job)
        assert job.status == JobStatus.VERIFIED

        # 3. Score
        result = score_and_decide(db, job)
        assert result["total"] >= 70
        assert job.score_total >= 70

        # 4. Check shortlisted
        if job.status == JobStatus.SHORTLISTED:
            # 5. Create application
            app = create_application_for_shortlisted(db, job)
            assert app is not None
            assert job.status == JobStatus.WAITING_APPROVAL

            # 6. First approval
            approval1, token1 = create_approval(db, app, job)
            ok, msg = validate_and_decide(db, token1, ApprovalDecision.APPROVED)
            assert ok
            assert job.status == JobStatus.APPROVED

            # 7. Prepare
            transition_job(db, job, JobStatus.PREPARING)
            app.state = JobStatus.PREPARING
            db.commit()

            transition_job(db, job, JobStatus.READY_TO_SUBMIT)
            app.state = JobStatus.READY_TO_SUBMIT
            db.commit()

            # 8. Submission guard — compute manifest
            guard = SubmissionGuard()
            snapshot = {
                "job_id": job.id,
                "company": job.company,
                "title": job.title,
                "url": job.canonical_url,
            }
            answers = {"full_name": "Maviz Ali", "email": "maviz.ali92@gmail.com"}
            manifest_hash = guard.compute_manifest_hash(snapshot, "profile/Maviz-Ali-Resume-Original.pdf", answers)

            # 9. Second approval (final)
            approval2, token2 = create_final_approval(db, app, job, manifest_hash)
            assert approval2.payload_hash == manifest_hash

            # Verify manifest hasn't changed
            current_hash = guard.compute_manifest_hash(snapshot, "profile/Maviz-Ali-Resume-Original.pdf", answers)
            assert guard.verify_manifest(current_hash, manifest_hash)

            # Decide second approval
            approval2.decision = ApprovalDecision.APPROVED
            approval2.decided_at = datetime.now(timezone.utc)
            db.commit()

            # 10. Dual approval check
            assert check_dual_approval(db, app.id)

            # 11. Submit
            transition_job(db, job, JobStatus.SUBMITTED)
            app.state = JobStatus.SUBMITTED
            app.submitted_at = datetime.now(timezone.utc)
            app.confirmation = "Submitted via dry-run test"
            db.commit()

            assert job.status == JobStatus.SUBMITTED
            assert app.confirmation is not None

        # 12. Second run — no duplicates
        dup = ingest_discovered_job(
            db,
            title="Forward Deployed Engineer",
            company="AI Startup",
            url="https://aistartup.com/careers/fde-001",
            source="greenhouse",
        )
        assert dup is None
        assert db.query(Job).filter(Job.company == "AI Startup").count() == 1

    def test_no_submit_without_two_approvals(self, db):
        job = ingest_discovered_job(
            db, "AI Engineer", "NeedApprovalCo",
            "https://needapproval.com/job1", "test",
            location="Karachi", description="LLM API integration, agentic AI, prompt engineering, RAG, automation",
        )
        verify_job(db, job)
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
