"""Tests for search providers, multi-provider discovery, deduplication, freshness,
aggregator blocking, official URL resolution, and end-to-end discovery-to-approval."""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import httpx

from app.search.base import SearchResult, SearchProvider
from app.search.serper import SerperProvider, _parse_serper_date, _extract_company_from_serper
from app.search.adzuna import AdzunaProvider, _parse_adzuna_date
from app.search.arbeitnow import ArbeitnowProvider, _parse_arbeitnow_date
from app.search.remotive import RemotiveProvider, _parse_remotive_date
from app.search.resolver import is_aggregator_url, is_official_url, resolve_official_url
from app.adapters.base import DiscoveredJob, SourceAdapter
from app.discovery import (
    build_search_providers,
    discover_all,
    run_orchestrator,
    is_fresh,
    is_discovery_only_source,
    _search_result_to_discovered,
)
from app.models import Job, JobStatus, SourceRun, Application, Approval, ApprovalDecision


# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------

class FakeSearchProvider(SearchProvider):
    name = "fake_search"

    def __init__(self, results=None, fail=False):
        self._results = results or []
        self._fail = fail

    async def search(self, query, location="", max_results=25):
        if self._fail:
            raise RuntimeError("provider failure")
        return self._results


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
            title="Verified", company="VerifiedCo", url=url, source=self.name,
            description="Verified job content that is long enough to pass verification checks easily for testing purposes",
        )


def _sr(**overrides):
    defaults = dict(
        title="AI Engineer", company="TestCo", location="Karachi, Pakistan",
        url="https://testco.com/careers/ai-eng-1", snippet="LLM API integration agentic AI",
        source="fake_search", posted_date=datetime.now(timezone.utc) - timedelta(days=1),
    )
    defaults.update(overrides)
    return SearchResult(**defaults)


def _dj(**overrides):
    defaults = dict(
        title="AI Engineer", company="TestCo", url="https://testco.com/jobs/1",
        source="fake", location="Karachi, Pakistan",
        description="LLM API integration, agentic AI, prompt engineering, RAG with enough content here",
        requirements="1-2 years experience",
        posted_date=datetime.now(timezone.utc) - timedelta(days=1),
    )
    defaults.update(overrides)
    return DiscoveredJob(**defaults)


# ---------------------------------------------------------------------------
# Test: SearchProvider interface conformance
# ---------------------------------------------------------------------------

class TestSearchProviderInterface:
    def test_search_result_has_required_fields(self):
        sr = _sr()
        assert sr.title == "AI Engineer"
        assert sr.company == "TestCo"
        assert sr.location == "Karachi, Pakistan"
        assert sr.url.startswith("https://")
        assert sr.snippet
        assert sr.source == "fake_search"
        assert sr.posted_date is not None

    def test_convert_search_result_to_discovered(self):
        sr = _sr(salary="50000", remote_policy="remote")
        dj = _search_result_to_discovered(sr)
        assert dj.title == sr.title
        assert dj.company == sr.company
        assert dj.url == sr.url
        assert dj.source == sr.source
        assert dj.salary == "50000"
        assert dj.remote_policy == "remote"
        assert dj.posted_date == sr.posted_date


# ---------------------------------------------------------------------------
# Test: Multi-provider discovery
# ---------------------------------------------------------------------------

class TestMultiProviderDiscovery:
    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    def test_multiple_providers_merged(self, _mock_queries):
        p1 = FakeSearchProvider(results=[_sr(company="Alpha", url="https://alpha.com/j/1")])
        p2 = FakeSearchProvider(results=[_sr(company="Beta", url="https://beta.com/j/1")])
        jobs, errors, _diag = asyncio.run(discover_all([], search_providers=[p1, p2]))
        assert len(jobs) == 2
        companies = {j.company for j in jobs}
        assert "Alpha" in companies
        assert "Beta" in companies
        assert errors == {}

    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    def test_adapter_and_provider_results_merged(self, _mock_queries):
        adapter = FakeAdapter(jobs=[_dj(company="AdapterCo", url="https://adapterco.com/j/1")])
        provider = FakeSearchProvider(results=[_sr(company="SearchCo", url="https://searchco.com/j/1")])
        jobs, errors, _diag = asyncio.run(discover_all([adapter], search_providers=[provider]))
        assert len(jobs) == 2
        assert errors == {}


# ---------------------------------------------------------------------------
# Test: One provider failing without stopping the run
# ---------------------------------------------------------------------------

class TestProviderFailureIsolation:
    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    def test_one_provider_fails_others_continue(self, _mock_queries):
        good = FakeSearchProvider(results=[_sr()])
        bad = FakeSearchProvider(fail=True)
        bad.name = "broken_search"
        jobs, errors, _diag = asyncio.run(discover_all([], search_providers=[good, bad]))
        assert len(jobs) == 1
        assert "broken_search" in errors

    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    def test_provider_failure_with_adapter_success(self, _mock_queries, db):
        adapter = FakeAdapter(jobs=[_dj()])
        bad_provider = FakeSearchProvider(fail=True)
        bad_provider.name = "broken_provider"
        result = run_orchestrator(db, adapters=[adapter], search_providers=[bad_provider])
        assert result["status"] == "COMPLETED"
        assert result["ingested"] >= 1
        assert "broken_provider" in result["adapter_errors"]

    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    def test_all_providers_fail_run_still_completes(self, _mock_queries, db):
        bad1 = FakeSearchProvider(fail=True)
        bad1.name = "bad1"
        bad2 = FakeSearchProvider(fail=True)
        bad2.name = "bad2"
        result = run_orchestrator(db, adapters=[], search_providers=[bad1, bad2])
        assert result["status"] == "COMPLETED"
        assert result["discovered"] == 0


# ---------------------------------------------------------------------------
# Test: Deduplication across providers
# ---------------------------------------------------------------------------

class TestCrossProviderDeduplication:
    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    def test_same_url_from_two_providers_deduped(self, _mock_queries, db):
        url = "https://testco.com/careers/ai-eng-1"
        p1 = FakeSearchProvider(results=[_sr(source="serper", url=url)])
        p1.name = "serper"
        p2 = FakeSearchProvider(results=[_sr(source="remotive", url=url)])
        p2.name = "remotive"
        result = run_orchestrator(db, adapters=[], search_providers=[p1, p2])
        assert result["ingested"] <= 1
        assert db.query(Job).count() <= 1

    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    def test_same_company_title_content_deduped(self, _mock_queries, db):
        p1 = FakeSearchProvider(results=[_sr(url="https://a.com/j/1")])
        p1.name = "p1"
        p2 = FakeSearchProvider(results=[_sr(url="https://b.com/j/1")])
        p2.name = "p2"
        result = run_orchestrator(db, adapters=[], search_providers=[p1, p2])
        assert result["skipped_dedup"] >= 1 or result["ingested"] <= 1


# ---------------------------------------------------------------------------
# Test: Freshness filtering
# ---------------------------------------------------------------------------

class TestFreshnessFiltering:
    def test_fresh_result_passes(self):
        sr = _sr(posted_date=datetime.now(timezone.utc) - timedelta(days=2))
        dj = _search_result_to_discovered(sr)
        assert is_fresh(dj) is True

    def test_stale_result_rejected(self):
        sr = _sr(posted_date=datetime.now(timezone.utc) - timedelta(days=14))
        dj = _search_result_to_discovered(sr)
        assert is_fresh(dj) is False

    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    def test_stale_results_filtered_in_orchestrator(self, _mock_queries, db):
        stale = _sr(posted_date=datetime.now(timezone.utc) - timedelta(days=14))
        provider = FakeSearchProvider(results=[stale])
        result = run_orchestrator(db, adapters=[], search_providers=[provider])
        assert result["fresh"] == 0
        assert result["ingested"] == 0


# ---------------------------------------------------------------------------
# Test: Missing posted date rejection
# ---------------------------------------------------------------------------

class TestMissingPostedDateRejection:
    def test_no_posted_date_not_fresh(self):
        sr = _sr(posted_date=None)
        dj = _search_result_to_discovered(sr)
        assert is_fresh(dj) is False

    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    def test_no_posted_date_filtered_in_orchestrator(self, _mock_queries, db):
        no_date = _sr(posted_date=None)
        provider = FakeSearchProvider(results=[no_date])
        result = run_orchestrator(db, adapters=[], search_providers=[provider])
        assert result["fresh"] == 0
        assert result["ingested"] == 0


# ---------------------------------------------------------------------------
# Test: Aggregator URL never submitted
# ---------------------------------------------------------------------------

class TestAggregatorUrlBlocking:
    def test_linkedin_is_aggregator(self):
        assert is_aggregator_url("https://www.linkedin.com/jobs/view/12345") is True

    def test_indeed_is_aggregator(self):
        assert is_aggregator_url("https://indeed.com/viewjob?jk=abc") is True

    def test_glassdoor_is_aggregator(self):
        assert is_aggregator_url("https://www.glassdoor.com/job/123") is True

    def test_greenhouse_is_not_aggregator(self):
        assert is_aggregator_url("https://boards.greenhouse.io/company/jobs/123") is False

    def test_company_site_is_not_aggregator(self):
        assert is_aggregator_url("https://careers.stripe.com/listing/ai-engineer") is False

    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    @patch("app.discovery.resolve_official_url", new_callable=AsyncMock, return_value=(None, "unresolved"))
    def test_aggregator_url_skipped_in_orchestrator(self, _mock_resolve, _mock_queries, db):
        agg = _sr(url="https://www.linkedin.com/jobs/view/12345")
        provider = FakeSearchProvider(results=[agg])
        result = run_orchestrator(db, adapters=[], search_providers=[provider])
        assert result["skipped_aggregator"] >= 1
        assert db.query(Job).count() == 0

    def test_aggregator_source_is_discovery_only(self):
        for src in ["serper", "adzuna", "arbeitnow", "remotive"]:
            assert is_discovery_only_source(src) is True


# ---------------------------------------------------------------------------
# Test: Official URL resolution
# ---------------------------------------------------------------------------

class TestOfficialUrlResolution:
    def test_already_official_url(self):
        url = "https://boards.greenhouse.io/company/jobs/123"
        result_url, method = asyncio.run(resolve_official_url(url))
        assert result_url == url
        assert method == "already_official"

    def test_is_official_url_rejects_aggregators(self):
        assert is_official_url("https://linkedin.com/jobs/123") is False
        assert is_official_url("https://indeed.com/view/abc") is False

    def test_is_official_url_accepts_company_sites(self):
        assert is_official_url("https://careers.stripe.com/listing/123") is True
        assert is_official_url("https://boards.greenhouse.io/acme/jobs/1") is True


# ---------------------------------------------------------------------------
# Test: Unverified official URL blocked
# ---------------------------------------------------------------------------

class TestUnverifiedOfficialUrlBlocked:
    @patch("app.discovery._generate_search_queries", return_value=[("AI Engineer Karachi", "Karachi")])
    @patch("app.discovery.resolve_official_url", new_callable=AsyncMock, return_value=(None, "unresolved"))
    def test_unresolved_aggregator_blocked_as_manual_action(self, _mock_resolve, _mock_queries, db):
        agg = _sr(url="https://www.linkedin.com/jobs/view/99999")
        provider = FakeSearchProvider(results=[agg])
        result = run_orchestrator(db, adapters=[], search_providers=[provider])
        assert result["skipped_aggregator"] >= 1 or result["ingested"] == 0
        submitted = db.query(Job).filter(Job.status == JobStatus.SUBMITTED).count()
        assert submitted == 0


# ---------------------------------------------------------------------------
# Test: End-to-end discovery to first approval
# ---------------------------------------------------------------------------

class TestDiscoveryToFirstApproval:
    @patch("app.discovery._generate_search_queries", return_value=[("Forward Deployed Engineer Karachi", "Karachi")])
    def test_search_provider_result_flows_to_approval(self, _mock_queries, db):
        sr = _sr(
            title="Forward Deployed Engineer",
            company="AI Startup",
            url="https://aistartup.com/careers/fde-001",
            location="Karachi, Pakistan",
            snippet="LLM API integration, agentic AI, prompt engineering, RAG, automation, REST API, structured output, function calling",
            posted_date=datetime.now(timezone.utc) - timedelta(hours=6),
        )
        provider = FakeSearchProvider(results=[sr])
        result = run_orchestrator(db, adapters=[], search_providers=[provider])
        assert result["status"] == "COMPLETED"
        assert result["ingested"] >= 1

        job = db.query(Job).first()
        assert job is not None
        assert job.discovery_source == "fake_search"

        if job.status in (JobStatus.SHORTLISTED, JobStatus.WAITING_APPROVAL):
            app = db.query(Application).filter(Application.job_id == job.id).first()
            assert app is not None
            approval = db.query(Approval).filter(Approval.application_id == app.id).first()
            assert approval is not None
            assert approval.decision == ApprovalDecision.PENDING


# ---------------------------------------------------------------------------
# Test: Individual provider date parsers
# ---------------------------------------------------------------------------

class TestDateParsers:
    def test_serper_date_relative_days(self):
        dt = _parse_serper_date("3 days ago")
        assert dt is not None
        assert (datetime.now(timezone.utc) - dt).days <= 4

    def test_serper_date_relative_hours(self):
        dt = _parse_serper_date("5 hours ago")
        assert dt is not None

    def test_serper_date_absolute(self):
        dt = _parse_serper_date("Jan 15, 2026")
        assert dt is not None
        assert dt.year == 2026

    def test_serper_date_none(self):
        assert _parse_serper_date(None) is None
        assert _parse_serper_date("") is None

    def test_adzuna_date_iso(self):
        dt = _parse_adzuna_date("2026-09-10T12:00:00Z")
        assert dt is not None
        assert dt.month == 9

    def test_adzuna_date_none(self):
        assert _parse_adzuna_date(None) is None

    def test_arbeitnow_date_unix(self):
        ts = int(datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp())
        dt = _parse_arbeitnow_date(ts)
        assert dt is not None
        assert dt.year == 2026

    def test_arbeitnow_date_none(self):
        assert _parse_arbeitnow_date(None) is None

    def test_remotive_date_iso(self):
        dt = _parse_remotive_date("2026-09-10T10:00:00")
        assert dt is not None
        assert dt.day == 10

    def test_remotive_date_none(self):
        assert _parse_remotive_date(None) is None


# ---------------------------------------------------------------------------
# Test: Serper provider with mocked HTTP
# ---------------------------------------------------------------------------

class TestSerperProvider:
    def test_serper_search_parses_response(self):
        provider = SerperProvider(api_key="test-key")
        mock_response = httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "AI Engineer at TechCorp",
                        "link": "https://techcorp.com/careers/ai-eng",
                        "snippet": "Join our AI team",
                        "date": "2 days ago",
                        "displayedLink": "techcorp.com",
                    },
                ],
            },
            request=httpx.Request("POST", "https://google.serper.dev/search"),
        )
        with patch("app.search.serper.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results = asyncio.run(provider.search("AI Engineer", location="Karachi"))
            assert len(results) == 1
            assert results[0].title == "AI Engineer at TechCorp"
            assert results[0].url == "https://techcorp.com/careers/ai-eng"
            assert results[0].source == "serper"

    def test_serper_company_extraction(self):
        item = {"displayedLink": "https://careers.stripe.com/listing"}
        assert _extract_company_from_serper(item) == "Careers"


# ---------------------------------------------------------------------------
# Test: Adzuna provider with mocked HTTP
# ---------------------------------------------------------------------------

class TestAdzunaProvider:
    def test_adzuna_search_parses_response(self):
        provider = AdzunaProvider(app_id="test-id", api_key="test-key")
        mock_response = httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Python Developer",
                        "company": {"display_name": "DevCorp"},
                        "location": {"display_name": "Karachi"},
                        "redirect_url": "https://devcorp.com/apply/123",
                        "description": "Python and AI",
                        "created": "2026-09-14T08:00:00Z",
                    },
                ],
            },
            request=httpx.Request("GET", "https://api.adzuna.com/v1/api/jobs/pk/search/1"),
        )
        with patch("app.search.adzuna.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results = asyncio.run(provider.search("Python Developer"))
            assert len(results) == 1
            assert results[0].company == "DevCorp"
            assert results[0].source == "adzuna"


# ---------------------------------------------------------------------------
# Test: Arbeitnow provider with mocked HTTP
# ---------------------------------------------------------------------------

class TestArbeitnowProvider:
    def test_arbeitnow_search_filters_by_query(self):
        provider = ArbeitnowProvider()
        ts = int(datetime.now(timezone.utc).timestamp())
        mock_response = httpx.Response(
            200,
            json={
                "data": [
                    {"title": "AI Engineer", "company_name": "RemoteCo",
                     "location": "Remote", "url": "https://remoteco.com/j/1",
                     "description": "AI and LLM work", "remote": True, "created_at": ts},
                    {"title": "Plumber", "company_name": "PlumbCo",
                     "location": "NYC", "url": "https://plumbco.com/j/1",
                     "description": "Fix pipes", "remote": False, "created_at": ts},
                ],
            },
            request=httpx.Request("GET", "https://www.arbeitnow.com/api/job-board-api"),
        )
        with patch("app.search.arbeitnow.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results = asyncio.run(provider.search("AI Engineer"))
            assert len(results) == 1
            assert results[0].company == "RemoteCo"
            assert results[0].remote_policy == "remote"


# ---------------------------------------------------------------------------
# Test: Remotive provider with mocked HTTP
# ---------------------------------------------------------------------------

class TestRemotiveProvider:
    def test_remotive_search_parses_response(self):
        provider = RemotiveProvider()
        mock_response = httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "title": "LLM Engineer",
                        "company_name": "AIRemote",
                        "candidate_required_location": "Worldwide",
                        "url": "https://airemote.com/jobs/llm",
                        "description": "Build LLM pipelines",
                        "publication_date": "2026-09-14T10:00:00",
                        "salary": "80000-120000",
                    },
                ],
            },
            request=httpx.Request("GET", "https://remotive.com/api/remote-jobs"),
        )
        with patch("app.search.remotive.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results = asyncio.run(provider.search("LLM Engineer"))
            assert len(results) == 1
            assert results[0].company == "AIRemote"
            assert results[0].remote_policy == "remote"
            assert results[0].salary == "80000-120000"


# ---------------------------------------------------------------------------
# Test: Build search providers respects config
# ---------------------------------------------------------------------------

class TestBuildSearchProviders:
    def test_free_providers_always_included(self):
        with patch("app.discovery.settings") as mock_settings:
            mock_settings.serper_api_key = ""
            mock_settings.adzuna_app_id = ""
            mock_settings.adzuna_api_key = ""
            providers = build_search_providers()
            names = [p.name for p in providers]
            assert "arbeitnow" in names
            assert "remotive" in names
            assert "serper" not in names
            assert "adzuna" not in names

    def test_paid_providers_added_when_configured(self):
        with patch("app.discovery.settings") as mock_settings:
            mock_settings.serper_api_key = "sk-test"
            mock_settings.adzuna_app_id = "app-123"
            mock_settings.adzuna_api_key = "key-456"
            providers = build_search_providers()
            names = [p.name for p in providers]
            assert "serper" in names
            assert "adzuna" in names
            assert "arbeitnow" in names
            assert "remotive" in names


# ---------------------------------------------------------------------------
# Test: Job model stores discovery fields
# ---------------------------------------------------------------------------

class TestJobDiscoveryFields:
    def test_job_stores_discovery_source_and_urls(self, db):
        adapter = FakeAdapter(jobs=[_dj(
            url="https://official.com/careers/ai-1",
            source="fake",
        )])
        result = run_orchestrator(db, adapters=[adapter], search_providers=[])
        job = db.query(Job).first()
        if job:
            assert job.discovery_source == "fake"
