"""Celery tasks for scheduled job discovery and processing."""

from celery import Celery
from celery.schedules import crontab

from app.config import settings

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
    },
)


@celery_app.task
def run_discovery():
    from app.database import SessionLocal
    from app.discovery import run_orchestrator

    db = SessionLocal()
    try:
        run_orchestrator(db)
    finally:
        db.close()
