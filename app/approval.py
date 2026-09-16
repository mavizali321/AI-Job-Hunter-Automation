"""Approval management — token generation, validation, hash binding, expiry."""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import (
    Approval, ApprovalDecision, ApprovalChannel, ApprovalType,
    Application, Job, JobStatus, SubmissionNonce,
)
from app.state_machine import transition_job, InvalidTransitionError


APPROVAL_EXPIRY_HOURS = 24
NONCE_EXPIRY_SECONDS = 300


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _compute_payload_hash(job: Job, application: Application) -> str:
    payload = json.dumps({
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "canonical_url": job.canonical_url,
        "score": job.score_total,
        "resume_variant": application.resume_variant,
        "answer_set_version": application.answer_set_version,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _generate_ref_code(token_hash: str) -> str:
    return token_hash[:8].upper()


def create_approval(
    db: Session,
    application: Application,
    job: Job,
    channel: ApprovalChannel = ApprovalChannel.DASHBOARD,
) -> tuple[Approval, str]:
    token = secrets.token_urlsafe(32)
    payload_hash = _compute_payload_hash(job, application)
    token_hash = _hash_token(token)

    approval = Approval(
        application_id=application.id,
        token_hash=token_hash,
        channel=channel,
        approval_type=ApprovalType.FIRST,
        decision=ApprovalDecision.PENDING,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=APPROVAL_EXPIRY_HOURS),
        payload_hash=payload_hash,
        approval_ref_code=_generate_ref_code(token_hash),
    )
    db.add(approval)
    db.commit()
    return approval, token


def validate_and_decide(
    db: Session,
    token: str,
    decision: ApprovalDecision,
) -> tuple[bool, str]:
    token_hash = _hash_token(token)
    approval = db.query(Approval).filter(Approval.token_hash == token_hash).first()
    if not approval:
        return False, "Invalid approval token"
    return _apply_decision(db, approval, decision)


def decide_by_ref_code(
    db: Session,
    ref_code: str,
    decision: ApprovalDecision,
) -> tuple[bool, str]:
    approval = (
        db.query(Approval)
        .filter(Approval.approval_ref_code == ref_code.upper())
        .filter(Approval.decision == ApprovalDecision.PENDING)
        .first()
    )
    if not approval:
        return False, f"No pending approval found for ref code {ref_code}"
    return _apply_decision(db, approval, decision)


def _apply_decision(
    db: Session,
    approval: Approval,
    decision: ApprovalDecision,
) -> tuple[bool, str]:
    if approval.decision != ApprovalDecision.PENDING:
        return False, f"Approval already decided: {approval.decision.value}"

    now = datetime.now(timezone.utc)
    expires = approval.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        approval.decision = ApprovalDecision.EXPIRED
        db.commit()
        return False, "Approval token has expired"

    application = db.query(Application).filter(Application.id == approval.application_id).first()
    if not application:
        return False, "Application not found"

    job = db.query(Job).filter(Job.id == application.job_id).first()
    if not job:
        return False, "Job not found"

    is_final = approval.approval_type == ApprovalType.FINAL

    if is_final:
        if not approval.payload_hash:
            return False, "Final approval missing manifest hash"
        if application.manifest_hash != approval.payload_hash:
            return False, "Manifest has changed since final approval was created — re-approval required"
    else:
        current_hash = _compute_payload_hash(job, application)
        if current_hash != approval.payload_hash:
            return False, "Payload has changed since approval was created — re-approval required"

    approval.decision = decision
    approval.decided_at = now

    if decision == ApprovalDecision.APPROVED:
        if is_final:
            target = JobStatus.FINAL_APPROVED
        else:
            target = JobStatus.APPROVED
        try:
            transition_job(db, job, target, {"approval_id": approval.id})
            application.state = target
            application.approval_version = approval.token_hash[:8]
        except InvalidTransitionError as e:
            return False, str(e)
    elif decision == ApprovalDecision.REJECTED:
        try:
            transition_job(db, job, JobStatus.REJECTED, {"approval_id": approval.id})
            application.state = JobStatus.REJECTED
        except InvalidTransitionError as e:
            return False, str(e)

    db.commit()
    return True, f"Approval {decision.value.lower()}"


def check_dual_approval(db: Session, application_id: int) -> bool:
    first = (
        db.query(Approval)
        .filter(
            Approval.application_id == application_id,
            Approval.approval_type == ApprovalType.FIRST,
            Approval.decision == ApprovalDecision.APPROVED,
        )
        .first()
    )
    final = (
        db.query(Approval)
        .filter(
            Approval.application_id == application_id,
            Approval.approval_type == ApprovalType.FINAL,
            Approval.decision == ApprovalDecision.APPROVED,
        )
        .first()
    )
    return first is not None and final is not None


def create_final_approval(
    db: Session,
    application: Application,
    job: Job,
    manifest_hash: str,
    channel: ApprovalChannel = ApprovalChannel.DASHBOARD,
) -> tuple[Approval, str]:
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)

    application.manifest_hash = manifest_hash
    db.flush()

    approval = Approval(
        application_id=application.id,
        token_hash=token_hash,
        channel=channel,
        approval_type=ApprovalType.FINAL,
        decision=ApprovalDecision.PENDING,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=APPROVAL_EXPIRY_HOURS),
        payload_hash=manifest_hash,
        approval_ref_code=_generate_ref_code(token_hash),
    )
    db.add(approval)
    db.commit()
    return approval, token


def create_submission_nonce(db: Session, application: Application, manifest_hash: str) -> tuple[str, str]:
    existing = (
        db.query(SubmissionNonce)
        .filter(SubmissionNonce.application_id == application.id, SubmissionNonce.consumed == False)
        .first()
    )
    if existing:
        db.delete(existing)
        db.flush()

    nonce_value = secrets.token_urlsafe(32)
    nonce = SubmissionNonce(
        application_id=application.id,
        nonce=nonce_value,
        manifest_hash=manifest_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=NONCE_EXPIRY_SECONDS),
    )
    db.add(nonce)
    db.commit()
    return nonce_value, nonce.manifest_hash


def consume_submission_nonce(
    db: Session, nonce_value: str, manifest_hash: str,
    application_id: int | None = None,
) -> tuple[bool, str]:
    nonce = db.query(SubmissionNonce).filter(SubmissionNonce.nonce == nonce_value).first()
    if not nonce:
        return False, "Invalid submission nonce"
    if application_id is not None and nonce.application_id != application_id:
        return False, "Nonce not bound to this application"
    if nonce.consumed:
        return False, "Nonce already consumed"

    now = datetime.now(timezone.utc)
    expires = nonce.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        return False, "Nonce has expired"

    if nonce.manifest_hash != manifest_hash:
        return False, "Manifest hash mismatch — content changed after authorization"

    nonce.consumed = True
    nonce.consumed_at = now
    db.commit()
    return True, "Nonce consumed"
