"""Celery tasks for scheduled job discovery and processing."""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

try:
    from celery import Celery
    from celery.schedules import crontab

    celery_app = Celery("jobhunter", broker=settings.redis_url)
    celery_app.conf.update(
        timezone=settings.timezone,
        beat_schedule={
            "morning-discovery": {
                "task": "app.tasks.run_discovery",
                "schedule": crontab(hour=9, minute=0),
            },
            "evening-discovery": {
                "task": "app.tasks.run_discovery",
                "schedule": crontab(hour=18, minute=0),
            },
            "recover-stuck-jobs": {
                "task": "app.tasks.recover_stuck_jobs",
                "schedule": crontab(minute="*/5"),
            },
        },
    )
    _celery_available = True
except ImportError:
    celery_app = None
    _celery_available = False
    logger.info("Celery not installed — tasks will run synchronously or be skipped")


def _task(func=None, **kwargs):
    """Decorator: registers with Celery when available, otherwise plain function."""
    if func is None:
        return lambda f: _task(f, **kwargs)
    if _celery_available:
        wrapped = celery_app.task(**kwargs)(func)
        return wrapped
    func.delay = func
    func.apply_async = lambda *a, **kw: func(*a, **kw)
    return func


@_task
def run_discovery():
    from app.database import SessionLocal
    from app.discovery import run_orchestrator

    db = SessionLocal()
    try:
        run_orchestrator(db)
    finally:
        db.close()


@_task(bind=_celery_available, max_retries=3, default_retry_delay=60)
def prepare_approved_application(*args):
    if _celery_available:
        self, job_id = args[0], args[1]
    else:
        self, job_id = None, args[0]

    from app.database import SessionLocal
    from app.models import Job, Application, Approval, ApprovalType, JobStatus

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job or job.status != JobStatus.APPROVED:
            return {"status": "skipped", "job_id": job_id}

        application = db.query(Application).filter(
            Application.job_id == job.id,
        ).first()
        if not application:
            return {"status": "no_application", "job_id": job_id}

        existing_final = db.query(Approval).filter(
            Approval.application_id == application.id,
            Approval.approval_type == ApprovalType.FINAL,
        ).first()
        if existing_final:
            return {"status": "already_prepared", "job_id": job_id}

        from app.pipeline import prepare_application

        result = prepare_application(db, job, application)
        if result is None:
            return {"status": "prepare_returned_none", "job_id": job_id}

        return {"status": "prepared", "job_id": job_id, "app_dir": str(result)}
    except Exception as exc:
        from app.models import Event

        try:
            event = Event(
                entity="preparation",
                entity_id=job_id,
                action="task_failed",
                metadata_={
                    "error": str(exc)[:200],
                    "attempt": getattr(
                        getattr(self, "request", None), "retries", 0,
                    ),
                },
            )
            db.add(event)
            db.commit()
        except Exception:
            pass
        if self is not None and hasattr(self, "retry"):
            raise self.retry(exc=exc)
        raise
    finally:
        db.close()


@_task
def recover_stuck_jobs():
    from datetime import datetime, timezone, timedelta
    from app.database import SessionLocal
    from app.models import Job, JobStatus
    from app.state_machine import transition_job

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)

        stuck_approved = db.query(Job).filter(
            Job.status == JobStatus.APPROVED,
            Job.updated_at < cutoff,
        ).all()
        for job in stuck_approved:
            prepare_approved_application.delay(job.id)

        stuck_preparing = db.query(Job).filter(
            Job.status == JobStatus.PREPARING,
            Job.updated_at < cutoff,
        ).all()
        for job in stuck_preparing:
            transition_job(
                db, job, JobStatus.FAILED,
                {"reason": "Preparation task stalled"},
            )
        if stuck_preparing:
            db.commit()

        return {
            "re_enqueued_approved": len(stuck_approved),
            "failed_preparing": len(stuck_preparing),
        }
    finally:
        db.close()
