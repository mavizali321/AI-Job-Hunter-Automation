"""Tests for approval system: expiry, hash binding, dual approval, no-submit-without-approval."""

import pytest
from datetime import datetime, timedelta, timezone

from app.models import Job, Application, Approval, JobStatus, ApprovalDecision, ApprovalChannel, ApprovalType
from app.approval import (
    create_approval, validate_and_decide, check_dual_approval,
    create_final_approval, _hash_token, _compute_payload_hash,
    create_submission_nonce, consume_submission_nonce,
)
from app.state_machine import transition_job
from local_worker.worker import SubmissionGuard


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
        assert approval.approval_type == ApprovalType.FIRST

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


class TestFinalApproval:
    def test_final_approval_uses_manifest_hash(self, db):
        job, app = _make_job_and_app(db, status=JobStatus.AWAITING_FINAL_APPROVAL)
        manifest = "abc123def456"
        approval, token = create_final_approval(db, app, job, manifest)
        assert approval.approval_type == ApprovalType.FINAL
        assert approval.payload_hash == manifest
        assert app.manifest_hash == manifest

    def test_final_approval_validates_against_manifest(self, db):
        job, app = _make_job_and_app(db, status=JobStatus.AWAITING_FINAL_APPROVAL)
        manifest = "abc123def456"
        approval, token = create_final_approval(db, app, job, manifest)
        ok, msg = validate_and_decide(db, token, ApprovalDecision.APPROVED)
        assert ok
        assert job.status == JobStatus.FINAL_APPROVED

    def test_final_approval_rejects_changed_manifest(self, db):
        job, app = _make_job_and_app(db, status=JobStatus.AWAITING_FINAL_APPROVAL)
        manifest = "abc123def456"
        approval, token = create_final_approval(db, app, job, manifest)
        app.manifest_hash = "CHANGED_HASH"
        db.commit()
        ok, msg = validate_and_decide(db, token, ApprovalDecision.APPROVED)
        assert not ok
        assert "Manifest has changed" in msg


class TestDualApproval:
    def test_single_approval_not_enough(self, db):
        job, app = _make_job_and_app(db)
        create_approval(db, app, job)
        assert not check_dual_approval(db, app.id)

    def test_dual_approval_passes(self, db):
        job, app = _make_job_and_app(db)
        _, t1 = create_approval(db, app, job)
        validate_and_decide(db, t1, ApprovalDecision.APPROVED)

        transition_job(db, job, JobStatus.PREPARING)
        db.commit()
        transition_job(db, job, JobStatus.READY_TO_SUBMIT)
        db.commit()
        transition_job(db, job, JobStatus.AWAITING_FINAL_APPROVAL)
        db.commit()

        guard = SubmissionGuard()
        snapshot = {"job_id": job.id, "company": job.company, "title": job.title}
        manifest_hash = guard.compute_manifest_hash(snapshot, "resume.pdf", {"name": "Maviz"})
        _, t2 = create_final_approval(db, app, job, manifest_hash)
        validate_and_decide(db, t2, ApprovalDecision.APPROVED)

        assert check_dual_approval(db, app.id)


class TestSubmissionNonce:
    def test_create_and_consume_nonce(self, db):
        job, app = _make_job_and_app(db)
        manifest = "test_manifest_hash"
        nonce_val, nonce_hash = create_submission_nonce(db, app, manifest)
        assert nonce_val
        assert nonce_hash == manifest

        ok, msg = consume_submission_nonce(db, nonce_val, manifest)
        assert ok

    def test_nonce_cannot_be_consumed_twice(self, db):
        job, app = _make_job_and_app(db)
        nonce_val, _ = create_submission_nonce(db, app, "hash")
        consume_submission_nonce(db, nonce_val, "hash")
        ok, msg = consume_submission_nonce(db, nonce_val, "hash")
        assert not ok
        assert "already consumed" in msg.lower()

    def test_nonce_rejects_wrong_hash(self, db):
        job, app = _make_job_and_app(db)
        nonce_val, _ = create_submission_nonce(db, app, "correct_hash")
        ok, msg = consume_submission_nonce(db, nonce_val, "wrong_hash")
        assert not ok
        assert "mismatch" in msg.lower()

    def test_expired_nonce_rejected(self, db):
        from app.models import SubmissionNonce as NonceModel
        job, app = _make_job_and_app(db)
        nonce_val, _ = create_submission_nonce(db, app, "hash")
        nonce = db.query(NonceModel).filter(NonceModel.nonce == nonce_val).first()
        nonce.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        ok, msg = consume_submission_nonce(db, nonce_val, "hash")
        assert not ok
        assert "expired" in msg.lower()


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

    def test_cannot_submit_from_ready_to_submit(self, db):
        from app.state_machine import InvalidTransitionError
        job = Job(
            canonical_url="https://example.com/no-direct",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.READY_TO_SUBMIT,
        )
        db.add(job)
        db.commit()

        with pytest.raises(InvalidTransitionError):
            transition_job(db, job, JobStatus.SUBMITTED)
