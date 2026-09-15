"""Tests for deduplication logic."""

from app.models import Job, JobStatus, canonicalize_url
from app.pipeline import ingest_discovered_job


class TestCanonicalize:
    def test_strips_trailing_slash(self):
        assert canonicalize_url("https://example.com/job/") == "https://example.com/job"

    def test_lowercases(self):
        assert canonicalize_url("HTTPS://Example.COM/Job") == "https://example.com/job"

    def test_strips_fragments(self):
        assert canonicalize_url("https://example.com/job#apply") == "https://example.com/job"

    def test_strips_query(self):
        assert canonicalize_url("https://example.com/job?ref=123") == "https://example.com/job"

    def test_empty(self):
        assert canonicalize_url("") == ""


class TestDedup:
    def test_same_url_deduped(self, db):
        job1 = ingest_discovered_job(db, "Engineer", "TestCo", "https://example.com/job1", "test")
        assert job1 is not None

        job2 = ingest_discovered_job(db, "Engineer", "TestCo", "https://example.com/job1", "test")
        assert job2 is None

    def test_same_company_title_hash_deduped(self, db):
        job1 = ingest_discovered_job(
            db, "AI Engineer", "CompanyX", "https://a.com/1", "test",
            description="Build AI features",
        )
        assert job1 is not None

        job2 = ingest_discovered_job(
            db, "AI Engineer", "CompanyX", "https://b.com/2", "test",
            description="Build AI features",
        )
        assert job2 is None

    def test_different_jobs_not_deduped(self, db):
        job1 = ingest_discovered_job(db, "Engineer A", "Co1", "https://a.com/1", "test", description="Role A")
        job2 = ingest_discovered_job(db, "Engineer B", "Co2", "https://b.com/2", "test", description="Role B")
        assert job1 is not None
        assert job2 is not None

    def test_second_run_no_duplicates(self, db):
        for _ in range(2):
            ingest_discovered_job(db, "AI Engineer", "TestCo", "https://example.com/j1", "test")
        count = db.query(Job).count()
        assert count == 1
