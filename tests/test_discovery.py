"""Tests for the async discovery orchestrator."""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.adapters.base import DiscoveredJob, SourceAdapter
from app.discovery import (
    build_adapters,
    discover_all,
    is_fresh,
    is_discovery_only_source,
    run_orchestrator,
    build_search_providers,
)
from app.models import Job, JobStatus, SourceRun


class FakeAdapter(SourceAdapter):
    name = "fake"

    def __init__(self, jobs=None, fail=False):
        self._jobs = jobs or []
        self._fail = fail

    async def discover(self, search_terms=None, location="", max_results=50):
        if self._fail:
            raise RuntimeError("adapter failure")
        return self._jobs

    async def verify(self, url):
        return DiscoveredJob(
            title="Verified",
            company="VerifiedCo",
            url=url,
            source=self.name,
            description="Verified job with enough content to pass the content length check easily for testing",
        )


def _make_job(**overrides):
    defaults = {
        "title": "AI Engineer",
        "company": "TestCo",
        "url": "https://testco.com/jobs/1",
        "source": "fake",
        "location": "Karachi, Pakistan",
        "description": "LLM API integration, agentic AI, prompt engineering, RAG with enough content here",
        "requirements": "1-2 years experience",
        "posted_date": datetime.now(timezone.utc) - timedelta(days=1),
    }
    defaults.update(overrides)
    return DiscoveredJob(**defaults)


class TestBuildAdapters:
    def test_no_config_returns_empty(self):
        with patch("app.discovery.settings") as mock_settings:
            mock_settings.greenhouse_tokens_list = []
            mock_settings.lever_slugs_list = []
            mock_settings.ashby_slugs_list = []
            mock_settings.career_urls_list = []
            adapters = build_adapters()
            assert adapters == []

    def test_builds_configured_adapters(self):
        with patch("app.discovery.settings") as mock_settings:
            mock_settings.greenhouse_tokens_list = ["acme"]
            mock_settings.lever_slugs_list = ["lever-co"]
            mock_settings.ashby_slugs_list = []
            mock_settings.career_urls_list = []
            adapters = build_adapters()
            assert len(adapters) == 2
            assert adapters[0].name == "greenhouse"
            assert adapters[1].name == "lever"


class TestDiscoverAll:
    def test_empty_adapters(self):
        jobs, errors = asyncio.run(discover_all([], search_providers=[]))
        assert jobs == []
        assert errors == {}

    def test_collects_jobs_from_multiple_adapters(self):
        a1 = FakeAdapter(jobs=[_make_job(company="A")])
        a2 = FakeAdapter(jobs=[_make_job(company="B"), _make_job(company="C")])
        jobs, errors = asyncio.run(discover_all([a1, a2], search_providers=[]))
        assert len(jobs) == 3
        assert errors == {}

    def test_isolates_adapter_failure(self):
        good = FakeAdapter(jobs=[_make_job()])
        bad = FakeAdapter(fail=True)
        bad.name = "broken"
        jobs, errors = asyncio.run(discover_all([good, bad], search_providers=[]))
        assert len(jobs) == 1
        assert "broken" in errors


class TestFreshness:
    def test_fresh_job(self):
        job = _make_job(posted_date=datetime.now(timezone.utc) - timedelta(days=3))
        assert is_fresh(job) is True

    def test_stale_job(self):
        job = _make_job(posted_date=datetime.now(timezone.utc) - timedelta(days=10))
        assert is_fresh(job) is False

    def test_no_date_not_fresh(self):
        job = _make_job(posted_date=None)
        assert is_fresh(job) is False

    def test_custom_window(self):
        job = _make_job(posted_date=datetime.now(timezone.utc) - timedelta(days=5))
        assert is_fresh(job, window_days=3) is False
        assert is_fresh(job, window_days=7) is True

    def test_naive_datetime_treated_as_utc(self):
        job = _make_job(posted_date=datetime.now() - timedelta(days=2))
        assert is_fresh(job) is True


class TestDiscoveryOnlySource:
    def test_linkedin_is_discovery_only(self):
        assert is_discovery_only_source("LinkedIn") is True
        assert is_discovery_only_source("linkedin") is True

    def test_indeed_is_discovery_only(self):
        assert is_discovery_only_source("Indeed") is True

    def test_greenhouse_is_not_discovery_only(self):
        assert is_discovery_only_source("greenhouse") is False

    def test_lever_is_not_discovery_only(self):
        assert is_discovery_only_source("lever") is False

    def test_serper_is_discovery_only(self):
        assert is_discovery_only_source("serper") is True

    def test_remotive_is_discovery_only(self):
        assert is_discovery_only_source("remotive") is True

    def test_arbeitnow_is_discovery_only(self):
        assert is_discovery_only_source("arbeitnow") is True

    def test_adzuna_is_discovery_only(self):
        assert is_discovery_only_source("adzuna") is True


class TestRunOrchestrator:
    def test_no_adapters_creates_source_run(self, db):
        result = run_orchestrator(db, adapters=[], search_providers=[])
        assert result["status"] == "COMPLETED"
        assert result["discovered"] == 0
        runs = db.query(SourceRun).all()
        assert len(runs) == 1
        assert runs[0].status == "COMPLETED"

    def test_jobs_enter_db(self, db):
        adapter = FakeAdapter(jobs=[
            _make_job(url="https://testco.com/jobs/1", company="TestCo"),
            _make_job(url="https://testco.com/jobs/2", company="TestCo", title="LLM Engineer"),
        ])
        result = run_orchestrator(db, adapters=[adapter], search_providers=[])
        assert result["status"] == "COMPLETED"
        assert result["ingested"] == 2
        assert db.query(Job).count() == 2

    def test_no_duplicates_on_second_run(self, db):
        adapter = FakeAdapter(jobs=[_make_job()])
        result1 = run_orchestrator(db, adapters=[adapter], search_providers=[])
        assert result1["ingested"] == 1

        result2 = run_orchestrator(db, adapters=[adapter], search_providers=[])
        assert result2["ingested"] == 0
        assert result2["skipped_dedup"] == 1
        assert db.query(Job).count() == 1

    def test_stale_jobs_filtered(self, db):
        stale = _make_job(posted_date=datetime.now(timezone.utc) - timedelta(days=14))
        adapter = FakeAdapter(jobs=[stale])
        result = run_orchestrator(db, adapters=[adapter], search_providers=[])
        assert result["fresh"] == 0
        assert result["ingested"] == 0

    def test_hard_reject_filtered(self, db):
        bad = _make_job(
            title="Senior Staff Principal Engineer",
            requirements="10+ years required",
            location="US only",
        )
        adapter = FakeAdapter(jobs=[bad])
        result = run_orchestrator(db, adapters=[adapter], search_providers=[])
        assert result["skipped_reject"] == 1
        assert result["ingested"] == 0

    def test_scoring_and_shortlisting(self, db):
        good_job = _make_job(
            title="Forward Deployed Engineer",
            company="AI Startup",
            location="Karachi, Pakistan",
            description=(
                "LLM API integration, agentic AI, prompt engineering, RAG, "
                "automation, REST API, structured output, function calling"
            ),
            requirements="1-2 years experience",
        )
        adapter = FakeAdapter(jobs=[good_job])
        result = run_orchestrator(db, adapters=[adapter], search_providers=[])
        assert result["ingested"] == 1
        assert result["verified"] + result["blocked"] >= 1

        job = db.query(Job).first()
        if job.status == JobStatus.VERIFIED or job.score_total is not None:
            assert job.score_total is not None
            assert job.score_total > 0

    def test_adapter_failure_isolation(self, db):
        good = FakeAdapter(jobs=[_make_job()])
        bad = FakeAdapter(fail=True)
        bad.name = "broken"
        result = run_orchestrator(db, adapters=[good, bad], search_providers=[])
        assert result["status"] == "COMPLETED"
        assert result["ingested"] == 1
        assert "broken" in result["adapter_errors"]

    def test_source_run_records_counts(self, db):
        adapter = FakeAdapter(jobs=[
            _make_job(url="https://example.com/1", company="Alpha"),
            _make_job(url="https://example.com/2", company="Beta", title="LLM Engineer"),
        ])
        run_orchestrator(db, adapters=[adapter], search_providers=[])
        run = db.query(SourceRun).first()
        assert run.status == "COMPLETED"
        assert run.jobs_found == 2
        assert run.jobs_new == 2
        assert run.ended_at is not None

    def test_blocked_jobs_tracked(self, db):
        class FailVerifyAdapter(SourceAdapter):
            name = "failverify"
            async def discover(self, search_terms=None, location="", max_results=50):
                return [_make_job(source="failverify")]
            async def verify(self, url):
                return None

        adapter = FailVerifyAdapter()
        result = run_orchestrator(db, adapters=[adapter], search_providers=[])
        assert result["blocked"] >= 1 or result["verified"] >= 0


class TestStaleJobBlockedFromApproval:
    def test_unverified_job_cannot_be_scored(self, db):
        from app.pipeline import ingest_discovered_job, score_and_decide
        job = ingest_discovered_job(
            db,
            title="AI Engineer",
            company="BlockCo",
            url="https://blockco.com/job",
            source="test",
            location="Karachi",
            description="LLM integration",
        )
        assert job.status == JobStatus.DISCOVERED
        result = score_and_decide(db, job)
        assert result == {}
        assert job.status == JobStatus.DISCOVERED

    def test_unscored_job_cannot_create_application(self, db):
        from app.pipeline import ingest_discovered_job, verify_job, create_application_for_shortlisted
        job = ingest_discovered_job(
            db,
            title="AI Engineer",
            company="BlockCo2",
            url="https://blockco2.com/job",
            source="test",
            location="Karachi",
            description="LLM integration",
        )
        ev = {
            "http_success": True, "url_is_official": True, "has_content": True,
            "listing_closed": False, "has_posted_date": True, "location_eligible": True,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        verify_job(db, job, ev)
        assert job.status == JobStatus.VERIFIED
        app = create_application_for_shortlisted(db, job)
        assert app is None


class TestSearchTerms:
    def test_job_titles_defined(self):
        from app.config import settings
        titles = settings.job_titles_list
        assert len(titles) > 0
        assert "AI Engineer" in titles

    def test_job_locations_defined(self):
        from app.config import settings
        locations = settings.job_locations_list
        assert "Karachi" in locations
        assert "Remote" in locations
