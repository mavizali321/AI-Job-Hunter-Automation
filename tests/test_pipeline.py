"""Tests for the job processing pipeline."""

from datetime import datetime, timezone

from app.models import Job, Application, JobStatus
from app.pipeline import (
    ingest_discovered_job, verify_job, score_and_decide,
    create_application_for_shortlisted, prepare_application,
    collect_verification_evidence, compute_verification_blockers,
)
from app.state_machine import transition_job


def _valid_evidence(**overrides):
    ev = {
        "http_success": True,
        "url_is_official": True,
        "has_content": True,
        "listing_closed": False,
        "has_posted_date": True,
        "location_eligible": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    ev.update(overrides)
    return ev


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
        assert verify_job(db, job, _valid_evidence())
        assert job.status == JobStatus.VERIFIED

    def test_verify_without_evidence_blocks(self, db):
        job = ingest_discovered_job(db, "Engineer", "Co2", "https://e2.com/1", "test")
        assert not verify_job(db, job)
        assert job.status == JobStatus.MANUAL_ACTION_REQUIRED
        assert job.verification_blockers is not None

    def test_score_and_decide(self, db):
        job = ingest_discovered_job(
            db, "AI Integration Engineer", "GoodCo",
            "https://e.com/2", "test",
            location="Karachi, Pakistan", description="LLM API integration, automation, prompt engineering, agentic AI, RAG",
        )
        verify_job(db, job, _valid_evidence())
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
        verify_job(db, job, _valid_evidence())
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


class TestVerificationEvidence:
    def test_collect_evidence_all_good(self):
        ev = collect_verification_evidence(
            url="https://company.com/jobs/1",
            posted_date=datetime.now(timezone.utc),
            location="Karachi, Pakistan",
            description="Full job description with enough content here to pass the length check easily",
            http_success=True,
        )
        assert ev["http_success"] is True
        assert ev["url_is_official"] is True
        assert ev["has_content"] is True
        assert ev["listing_closed"] is False
        assert ev["has_posted_date"] is True
        assert ev["location_eligible"] is True

    def test_collect_evidence_aggregator_url(self):
        ev = collect_verification_evidence(
            url="https://linkedin.com/jobs/123",
            posted_date=datetime.now(timezone.utc),
            location="Karachi",
            description="Some job",
            http_success=True,
        )
        assert ev["url_is_official"] is False

    def test_collect_evidence_missing_date(self):
        ev = collect_verification_evidence(
            url="https://company.com/job",
            posted_date=None,
            location="Karachi",
            description="Some job description with enough detail to pass the content check easily",
            http_success=True,
        )
        assert ev["has_posted_date"] is False

    def test_collect_evidence_closed_listing(self):
        ev = collect_verification_evidence(
            url="https://company.com/job",
            posted_date=datetime.now(timezone.utc),
            location="Karachi",
            description="This position is closed and no longer accepting applications from here on",
            http_success=True,
        )
        assert ev["listing_closed"] is True

    def test_collect_evidence_empty_content(self):
        ev = collect_verification_evidence(
            url="https://company.com/job",
            posted_date=datetime.now(timezone.utc),
            location="Karachi",
            description="",
            http_success=True,
        )
        assert ev["has_content"] is False

    def test_compute_blockers_all_good(self):
        ev = _valid_evidence()
        assert compute_verification_blockers(ev) == []

    def test_compute_blockers_http_fail(self):
        ev = _valid_evidence(http_success=False)
        blockers = compute_verification_blockers(ev)
        assert any("HTTP" in b for b in blockers)

    def test_compute_blockers_missing_date(self):
        ev = _valid_evidence(has_posted_date=False)
        blockers = compute_verification_blockers(ev)
        assert any("posting date" in b.lower() for b in blockers)

    def test_compute_blockers_no_content(self):
        ev = _valid_evidence(has_content=False)
        blockers = compute_verification_blockers(ev)
        assert any("content" in b.lower() for b in blockers)

    def test_compute_blockers_location_ineligible(self):
        ev = _valid_evidence(location_eligible=False)
        blockers = compute_verification_blockers(ev)
        assert any("eligibility" in b.lower() for b in blockers)


class TestMissingDateBlocking:
    def test_missing_date_blocks_verification(self, db):
        job = ingest_discovered_job(
            db, "AI Engineer", "DatelessInc",
            "https://dateless.com/job", "test",
            location="Karachi, Pakistan",
            description="Good description with enough content here to pass",
        )
        ev = _valid_evidence(has_posted_date=False)
        result = verify_job(db, job, ev)
        assert result is False
        assert job.status == JobStatus.MANUAL_ACTION_REQUIRED
        assert "posting date" in job.verification_blockers.lower()

    def test_missing_date_cannot_be_shortlisted(self, db):
        job = ingest_discovered_job(
            db, "AI Engineer", "DatelessCo2",
            "https://dateless2.com/job", "test",
            location="Karachi, Pakistan",
            description="Good description with enough content",
        )
        ev = _valid_evidence(has_posted_date=False)
        verify_job(db, job, ev)
        assert job.status == JobStatus.MANUAL_ACTION_REQUIRED
        result = score_and_decide(db, job)
        assert result == {}


class TestSensitiveFields:
    def test_captcha_blocks(self):
        from app.scoring import SENSITIVE_FIELDS
        assert "visa" in SENSITIVE_FIELDS
        assert "work authorization" in SENSITIVE_FIELDS
        assert "salary expectation" in SENSITIVE_FIELDS
