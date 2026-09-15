"""Tests for approval system: expiry, hash binding, dual approval, no-submit-without-approval."""

import pytest
from datetime import datetime, timedelta, timezone

from app.models import Job, Application, Approval, JobStatus, ApprovalDecision, ApprovalChannel
from app.approval import (
    create_approval, validate_and_decide, check_dual_approval,
    create_final_approval, _hash_token, _compute_payload_hash,
)
from app.state_machine import transition_job


def _make_job_and_app(db, status=JobStatus.WAITING_APPROVAL):
    job = Job(
        canonical_url="https://example.com/approval-test",
        source="test", company="TestCo", title="AI Engineer",
        location="Karachi", score_total=85, status=status,
    )
    db.add(job)
    db.commit()
    app = Application(
        job_id=job.id,
        resume_variant="profile/Maviz-Ali-Resume-Original.pdf",
        state=status,
    )
    db.add(app)
    db.commit()
    return job, app


class TestApprovalCreation:
    def test_creates_approval_with_token(self, db):
        job, app = _make_job_and_app(db)
        approval, token = create_approval(db, app, job)
        assert approval.id is not None
        assert token
        assert approval.decision == ApprovalDecision.PENDING
        assert approval.payload_hash

    def test_payload_hash_stable(self, db):
        job, app = _make_job_and_app(db)
        h1 = _compute_payload_hash(job, app)
        h2 = _compute_payload_hash(job, app)
        assert h1 == h2


class TestApprovalValidation:
    def test_approve_valid_token(self, db):
        job, app = _make_job_and_app(db)
        approval, token = create_approval(db, app, job)
        ok, msg = validate_and_decide(db, token, ApprovalDecision.APPROVED)
        assert ok
        assert job.status == JobStatus.APPROVED

    def test_reject_valid_token(self, db):
        job, app = _make_job_and_app(db)
        approval, token = create_approval(db, app, job)
        ok, msg = validate_and_decide(db, token, ApprovalDecision.REJECTED)
        assert ok
        assert job.status == JobStatus.REJECTED

    def test_invalid_token_rejected(self, db):
        ok, msg = validate_and_decide(db, "bogus-token", ApprovalDecision.APPROVED)
        assert not ok
        assert "Invalid" in msg

    def test_expired_token_rejected(self, db):
        job, app = _make_job_and_app(db)
        approval, token = create_approval(db, app, job)
        approval.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()

        ok, msg = validate_and_decide(db, token, ApprovalDecision.APPROVED)
        assert not ok
        assert "expired" in msg.lower()

    def test_double_decide_rejected(self, db):
        job, app = _make_job_and_app(db)
        approval, token = create_approval(db, app, job)
        validate_and_decide(db, token, ApprovalDecision.APPROVED)
        ok, msg = validate_and_decide(db, token, ApprovalDecision.APPROVED)
        assert not ok
        assert "already decided" in msg.lower()

    def test_payload_change_invalidates(self, db):
        job, app = _make_job_and_app(db)
        approval, token = create_approval(db, app, job)

        job.score_total = 99
        db.commit()

        ok, msg = validate_and_decide(db, token, ApprovalDecision.APPROVED)
        assert not ok
        assert "changed" in msg.lower()


class TestDualApproval:
    def test_single_approval_not_enough(self, db):
        job, app = _make_job_and_app(db)
        create_approval(db, app, job)
        assert not check_dual_approval(db, app.id)

    def test_dual_approval_passes(self, db):
        job, app = _make_job_and_app(db)
        _, t1 = create_approval(db, app, job)
        validate_and_decide(db, t1, ApprovalDecision.APPROVED)

        job.status = JobStatus.WAITING_APPROVAL
        db.commit()
        _, t2 = create_approval(db, app, job)
        validate_and_decide(db, t2, ApprovalDecision.APPROVED)

        assert check_dual_approval(db, app.id)


class TestNoSubmitWithoutApproval:
    def test_cannot_reach_submitted_from_discovered(self, db):
        from app.state_machine import InvalidTransitionError
        job = Job(
            canonical_url="https://example.com/no-submit",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.DISCOVERED,
        )
        db.add(job)
        db.commit()

        with pytest.raises(InvalidTransitionError):
            transition_job(db, job, JobStatus.SUBMITTED)

    def test_cannot_skip_approval(self, db):
        from app.state_machine import InvalidTransitionError
        job = Job(
            canonical_url="https://example.com/no-skip-approval",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.SHORTLISTED,
        )
        db.add(job)
        db.commit()

        with pytest.raises(InvalidTransitionError):
            transition_job(db, job, JobStatus.SUBMITTED)
