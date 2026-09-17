"""Tests for batch provider behavior: single-fetch, tokenized matching,
partial-failure survival, query dedup, global limit, and error diagnostics."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from app.search.base import SearchResult, SearchProvider, ProviderDiagnostics
from app.search.arbeitnow import ArbeitnowProvider, _parse_arbeitnow_date
from app.search.remotive import RemotiveProvider
from app.discovery import discover_all


def _ts():
    return int(datetime.now(timezone.utc).timestamp())


def _arbeitnow_feed(items):
    return httpx.Response(
        200,
        json={"data": items},
        request=httpx.Request("GET", "https://www.arbeitnow.com/api/job-board-api"),
    )


def _remotive_feed(jobs):
    return httpx.Response(
        200,
        json={"jobs": jobs},
        request=httpx.Request("GET", "https://remotive.com/api/remote-jobs"),
    )


FEED_ITEMS = [
    {
        "title": "AI Engineer",
        "company_name": "AlphaCo",
        "description": "Build AI systems with machine learning and LLM integration",
        "url": "https://alphaco.com/jobs/1",
        "location": "Remote",
        "remote": True,
        "created_at": _ts(),
    },
    {
        "title": "Backend Developer",
        "company_name": "BetaCo",
        "description": "Python backend services, REST APIs, microservices",
        "url": "https://betaco.com/jobs/2",
        "location": "Berlin, Germany",
        "remote": False,
        "created_at": _ts(),
    },
    {
        "title": "Machine Learning Engineer",
        "company_name": "GammaCo",
        "description": "Deploy ML models, build AI pipelines, Python",
        "url": "https://gammaco.com/jobs/3",
        "location": "Worldwide",
        "remote": True,
        "created_at": _ts(),
    },
    {
        "title": "Data Scientist",
        "company_name": "DeltaCo",
        "description": "Statistical analysis and machine learning",
        "url": "https://deltaco.com/jobs/4",
        "location": "Karachi, Pakistan",
        "remote": False,
        "created_at": _ts(),
    },
]


class TestArbeitnowSingleFetch:
    def test_one_http_request_per_run(self):
        """Arbeitnow makes exactly ONE HTTP request regardless of query count."""
        provider = ArbeitnowProvider()
        queries = [
            ("AI Engineer", "Karachi"),
            ("Machine Learning", "Remote"),
            ("Data Scientist", "Pakistan"),
            ("Backend Developer", "Berlin"),
        ]

        with patch("app.search.arbeitnow.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = _arbeitnow_feed(FEED_ITEMS)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results, diag = asyncio.run(
                provider.search_batch(queries, max_results=100),
            )
            assert mock_instance.get.call_count == 1
            assert diag.requests_made == 1
            assert diag.status == "ok"
            assert len(results) > 0


class TestArbeitnowTokenizedMatching:
    def test_role_tokens_match_without_location_in_phrase(self):
        """Role tokens 'AI' and 'Engineer' match title/description independently
        of whether the location appears in the search phrase."""
        provider = ArbeitnowProvider()

        with patch("app.search.arbeitnow.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = _arbeitnow_feed(FEED_ITEMS)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results, diag = asyncio.run(
                provider.search_batch(
                    [("AI Engineer", "Remote")], max_results=100,
                ),
            )
            assert any(r.title == "AI Engineer" for r in results)
            assert not any(r.title == "Backend Developer" for r in results)

    def test_location_filtered_separately(self):
        """Location filtering is separate from role matching — Berlin-only
        non-remote job is excluded when searching for Karachi."""
        provider = ArbeitnowProvider()

        with patch("app.search.arbeitnow.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = _arbeitnow_feed(FEED_ITEMS)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results, _ = asyncio.run(
                provider.search_batch(
                    [("Backend Developer", "Karachi")], max_results=100,
                ),
            )
            assert not any(r.company == "BetaCo" for r in results)


class TestPartialResultsSurviveFailure:
    def test_partial_results_survive_one_query_failure(self):
        """When one query in a batch fails, results from other queries
        are still returned (via the base class search_batch)."""

        class PartialProvider(SearchProvider):
            name = "partial_test"
            call_count = 0

            async def search(self, query, location="", max_results=25):
                self.call_count += 1
                if "fail" in query.lower():
                    raise RuntimeError("Simulated HTTP failure")
                return [
                    SearchResult(
                        title=f"Job for {query}",
                        company="Co",
                        location=location,
                        url=f"https://co.com/{query.replace(' ', '-')}/1",
                        snippet="Good job",
                        source="partial_test",
                        posted_date=datetime.now(timezone.utc),
                    )
                ]

        provider = PartialProvider()
        queries = [
            ("AI Engineer", "Remote"),
            ("FAIL query", "Remote"),
            ("Data Scientist", "Remote"),
        ]

        results, diag = asyncio.run(
            provider.search_batch(queries, max_results=100),
        )
        assert len(results) == 2
        assert diag.status == "partial"
        assert len(diag.errors) == 1
        assert "FAIL query" in diag.errors[0]


class TestRemotiveQueryDeduplication:
    def test_remotive_deduplicates_identical_role_queries(self):
        """The same role paired with different locations should trigger
        only ONE API call, not one per location."""
        provider = RemotiveProvider()
        queries = [
            ("AI Engineer", "Karachi"),
            ("AI Engineer", "Remote"),
            ("AI Engineer", "Pakistan"),
            ("Data Scientist", "Karachi"),
            ("Data Scientist", "Remote"),
        ]

        call_count = 0
        original_search = provider.search

        async def counting_search(query, location="", max_results=25):
            nonlocal call_count
            call_count += 1
            return [
                SearchResult(
                    title=f"{query} role",
                    company="RemoteCo",
                    location="Worldwide",
                    url=f"https://remotive.com/jobs/{query.replace(' ', '-')}/{call_count}",
                    snippet="Remote work",
                    source="remotive",
                    posted_date=datetime.now(timezone.utc),
                    remote_policy="remote",
                )
            ]

        provider.search = counting_search
        results, diag = asyncio.run(
            provider.search_batch(queries, max_results=100),
        )
        assert call_count == 2
        assert diag.requests_made == 2
        assert len(results) == 2


class TestGlobalResultLimit:
    @patch("app.discovery._generate_search_queries", return_value=[("Dev", "Remote")])
    def test_global_limit_across_providers(self, _mock_queries):
        """MAX_RESULTS_PER_RUN caps total results across all providers."""
        from app.config import settings as real_settings

        class FloodProvider(SearchProvider):
            _counter = 0

            def __init__(self, tag):
                self.name = f"flood_{tag}"
                self.tag = tag

            async def search(self, query, location="", max_results=25):
                out = []
                for i in range(max_results):
                    FloodProvider._counter += 1
                    out.append(SearchResult(
                        title=f"Job {self.tag}-{FloodProvider._counter}",
                        company=f"Co-{self.tag}",
                        location="Remote",
                        url=f"https://{self.tag}.com/j/{FloodProvider._counter}",
                        snippet="desc",
                        source=self.name,
                        posted_date=datetime.now(timezone.utc),
                    ))
                return out

        FloodProvider._counter = 0
        original = real_settings.max_results_per_run
        try:
            object.__setattr__(real_settings, "max_results_per_run", 5)
            jobs, errors, diag = asyncio.run(discover_all(
                adapters=[],
                search_providers=[FloodProvider("a"), FloodProvider("b")],
            ))
            assert len(jobs) <= 5
        finally:
            object.__setattr__(real_settings, "max_results_per_run", original)


class TestProviderErrorDiagnostics:
    def test_http_error_includes_status_code(self):
        """Provider diagnostics include the HTTP status code on failure."""
        provider = ArbeitnowProvider()

        error_resp = httpx.Response(
            503,
            request=httpx.Request("GET", "https://www.arbeitnow.com/api/job-board-api"),
        )

        with patch("app.search.arbeitnow.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = error_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results, diag = asyncio.run(
                provider.search_batch(
                    [("AI Engineer", "Remote")], max_results=10,
                ),
            )
            assert diag.status == "failed"
            assert len(diag.errors) == 1
            assert "503" in diag.errors[0] or "HTTP" in diag.errors[0]
            assert results == []

    def test_timeout_error_includes_detail(self):
        """Provider diagnostics include timeout info on connection failure."""
        provider = ArbeitnowProvider()

        with patch("app.search.arbeitnow.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.side_effect = httpx.ConnectTimeout("Connection timed out")
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results, diag = asyncio.run(
                provider.search_batch(
                    [("AI Engineer", "Remote")], max_results=10,
                ),
            )
            assert diag.status == "failed"
            assert len(diag.errors) == 1
            assert "timeout" in diag.errors[0].lower() or "connect" in diag.errors[0].lower()
            assert results == []

    @patch("app.discovery._generate_search_queries", return_value=[("Dev", "Remote")])
    def test_diagnostics_included_in_orchestrator_summary(self, _mock_queries, db):
        """run_orchestrator includes provider_diagnostics in its summary."""
        from app.discovery import run_orchestrator

        class DiagProvider(SearchProvider):
            name = "diag_test"

            async def search(self, query, location="", max_results=25):
                return [
                    SearchResult(
                        title="Test Job", company="TestCo", location="Remote",
                        url="https://testco.com/j/1", snippet="test",
                        source="diag_test",
                        posted_date=datetime.now(timezone.utc),
                    )
                ]

        result = run_orchestrator(db, adapters=[], search_providers=[DiagProvider()])
        assert "provider_diagnostics" in result
        assert len(result["provider_diagnostics"]) >= 1
        diag = result["provider_diagnostics"][0]
        assert diag["provider"] == "diag_test"
        assert diag["requests_made"] >= 1
        assert "status" in diag
