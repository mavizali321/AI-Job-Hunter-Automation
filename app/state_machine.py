from app.models import JobStatus, Event
from sqlalchemy.orm import Session
from datetime import datetime, timezone


VALID_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.DISCOVERED: {
        JobStatus.VERIFIED, JobStatus.SKIPPED, JobStatus.EXPIRED, JobStatus.FAILED,
        JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.BLOCKED,
    },
    JobStatus.VERIFIED: {
        JobStatus.SCORED, JobStatus.SKIPPED, JobStatus.EXPIRED, JobStatus.FAILED,
    },
    JobStatus.SCORED: {
        JobStatus.SHORTLISTED, JobStatus.SKIPPED, JobStatus.EXPIRED,
        JobStatus.MANUAL_ACTION_REQUIRED,
    },
    JobStatus.SHORTLISTED: {
        JobStatus.WAITING_APPROVAL, JobStatus.SKIPPED, JobStatus.EXPIRED,
    },
    JobStatus.WAITING_APPROVAL: {
        JobStatus.APPROVED, JobStatus.REJECTED, JobStatus.EXPIRED,
    },
    JobStatus.APPROVED: {
        JobStatus.PREPARING, JobStatus.FAILED, JobStatus.EXPIRED,
    },
    JobStatus.PREPARING: {
        JobStatus.READY_TO_SUBMIT, JobStatus.BLOCKED, JobStatus.FAILED,
        JobStatus.MANUAL_ACTION_REQUIRED,
    },
    JobStatus.READY_TO_SUBMIT: {
        JobStatus.AWAITING_FINAL_APPROVAL, JobStatus.FAILED, JobStatus.BLOCKED,
        JobStatus.MANUAL_ACTION_REQUIRED,
    },
    JobStatus.AWAITING_FINAL_APPROVAL: {
        JobStatus.FINAL_APPROVED, JobStatus.REJECTED, JobStatus.EXPIRED,
        JobStatus.MANUAL_ACTION_REQUIRED,
    },
    JobStatus.FINAL_APPROVED: {
        JobStatus.SUBMITTED, JobStatus.FAILED, JobStatus.BLOCKED,
        JobStatus.MANUAL_ACTION_REQUIRED,
    },
    JobStatus.SUBMITTED: set(),
    JobStatus.REJECTED: set(),
    JobStatus.SKIPPED: set(),
    JobStatus.EXPIRED: set(),
    JobStatus.BLOCKED: {
        JobStatus.PREPARING, JobStatus.READY_TO_SUBMIT, JobStatus.MANUAL_ACTION_REQUIRED,
    },
    JobStatus.MANUAL_ACTION_REQUIRED: {
        JobStatus.PREPARING, JobStatus.READY_TO_SUBMIT, JobStatus.SCORED,
        JobStatus.BLOCKED, JobStatus.SKIPPED, JobStatus.VERIFIED,
        JobStatus.AWAITING_FINAL_APPROVAL,
    },
    JobStatus.FAILED: {
        JobStatus.DISCOVERED, JobStatus.PREPARING, JobStatus.READY_TO_SUBMIT,
    },
}


class InvalidTransitionError(Exception):
    def __init__(self, current: JobStatus, target: JobStatus):
        self.current = current
        self.target = target
        super().__init__(f"Invalid transition: {current.value} -> {target.value}")


def transition(
    db: Session,
    entity: str,
    entity_id: int,
    current_status: JobStatus,
    new_status: JobStatus,
    metadata: dict | None = None,
) -> Event:
    if new_status not in VALID_TRANSITIONS.get(current_status, set()):
        raise InvalidTransitionError(current_status, new_status)

    event = Event(
        entity=entity,
        entity_id=entity_id,
        action=f"{current_status.value}->{new_status.value}",
        old_state=current_status.value,
        new_state=new_status.value,
        metadata_=metadata,
        timestamp=datetime.now(timezone.utc),
    )
    db.add(event)
    return event


def transition_job(db: Session, job, new_status: JobStatus, metadata: dict | None = None):
    event = transition(db, "job", job.id, job.status, new_status, metadata)
    job.status = new_status
    return event
