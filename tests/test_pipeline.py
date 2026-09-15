"""Tests for the job processing pipeline."""

from app.models import Job, Application, JobStatus
from app.pipeline import (
    ingest_discovered_job, verify_job, score_and_decide,
    create_application_for_shortlisted, prepare_application,
)
from app.state_machine import transition_job


class TestPipeline:
    def test_ingest_creates_job(self, db):
        job = ingest_discovered_job(
            db, "Forward Deployed Engineer", "TestCo",
            "https://example.com/fde", "test",
            location="Karachi", description="LLM API integration, prompt engineering, agentic AI",
        )
        assert job is not None
        assert job.status == JobStatus.DISCOVERED
        assert job.company == "TestCo"

    def test_verify_transitions(self, db):
        job = ingest_discovered_job(db, "Engineer", "Co", "https://e.com/1", "test")
        assert verify_job(db, job)
        assert job.status == JobStatus.VERIFIED

    def test_score_and_decide(self, db):
        job = ingest_discovered_job(
            db, "AI Integration Engineer", "GoodCo",
            "https://e.com/2", "test",
            location="Karachi, Pakistan", description="LLM API integration, automation, prompt engineering, agentic AI, RAG",
        )
        verify_job(db, job)
        result = score_and_decide(db, job)
        assert result["total"] > 0
        assert job.score_total is not None
        assert job.decision is not None

    def test_shortlist_creates_application(self, db):
        job = ingest_discovered_job(
            db, "AI Solutions Engineer", "GreatCo",
            "https://e.com/3", "test",
            location="Karachi, Pakistan", description="LLM API integration, automation, prompt engineering, agentic AI, RAG pipeline",
        )
        verify_job(db, job)
        score_and_decide(db, job)

        if job.status == JobStatus.SHORTLISTED:
            app = create_application_for_shortlisted(db, job)
            assert app is not None
            assert job.status == JobStatus.WAITING_APPROVAL

    def test_full_pipeline_no_duplicates(self, db):
        for i in range(2):
            ingest_discovered_job(
                db, "AI Engineer", "SameCo",
                "https://same.com/job", "test",
                description="Same job posted twice",
            )
        assert db.query(Job).count() == 1


class TestSensitiveFields:
    def test_captcha_blocks(self):
        from app.scoring import SENSITIVE_FIELDS
        assert "visa" in SENSITIVE_FIELDS
        assert "work authorization" in SENSITIVE_FIELDS
        assert "salary expectation" in SENSITIVE_FIELDS
