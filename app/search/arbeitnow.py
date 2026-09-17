"""Arbeitnow public API provider — free, no key required.

Fetches the public feed ONCE per discovery run and filters locally for all
role × location queries.  Remote jobs are accepted only when location
indicates Pakistan or worldwide eligibility.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.search.base import SearchProvider, SearchResult, ProviderDiagnostics
from app.search.retry import request_with_retry, format_provider_error

logger = logging.getLogger(__name__)

ARBEITNOW_ENDPOINT = "https://www.arbeitnow.com/api/job-board-api"

PAKISTAN_MARKERS = {"pakistan", "karachi", "lahore", "islamabad"}
REMOTE_WORLDWIDE = {"worldwide", "global", "anywhere", "all countries"}


def _is_remote_eligible(item_location: str) -> bool:
    loc = item_location.lower().strip()
    if not loc or loc == "remote":
        return False
    if any(m in loc for m in REMOTE_WORLDWIDE):
        return True
    if any(m in loc for m in PAKISTAN_MARKERS):
        return True
    return False


class ArbeitnowProvider(SearchProvider):
    name = "arbeitnow"

    async def search(
        self,
        query: str,
        location: str = "",
        max_results: int = 25,
    ) -> list[SearchResult]:
        results, _ = await self.search_batch(
            [(query, location)], max_results=max_results,
        )
        return results

    async def search_batch(
        self,
        queries: list[tuple[str, str]],
        max_results: int = 100,
    ) -> tuple[list[SearchResult], ProviderDiagnostics]:
        diag = ProviderDiagnostics(provider=self.name)
        diag.requests_made = 1

        try:
            feed = await self._fetch_feed()
        except Exception as e:
            detail = format_provider_error(e)
            diag.errors.append(detail)
            diag.status = "failed"
            return [], diag

        items = feed.get("data", [])
        diag.results_received = len(items)

        seen_urls: set[str] = set()
        results: list[SearchResult] = []

        for query, location in queries:
            if len(results) >= max_results:
                break
            tokens = [
                t.strip().lower()
                for t in query.lower().split()
                if len(t.strip()) > 1
            ]
            for item in items:
                if len(results) >= max_results:
                    break
                url = item.get("url", "")
                if url in seen_urls:
                    continue

                title = item.get("title", "")
                desc = item.get("description", "")
                searchable = f"{title} {desc}".lower()
                if not all(tok in searchable for tok in tokens):
                    continue

                if not self._location_eligible(item, location):
                    continue

                seen_urls.add(url)
                posted = _parse_arbeitnow_date(item.get("created_at"))
                remote = "remote" if item.get("remote", False) else ""
                results.append(SearchResult(
                    title=title,
                    company=item.get("company_name", ""),
                    location=item.get("location", ""),
                    url=url,
                    snippet=desc[:500],
                    source="arbeitnow",
                    posted_date=posted,
                    remote_policy=remote,
                    raw_data=item,
                ))

        diag.results_accepted = len(results)
        if not diag.errors:
            diag.status = "ok"
        return results, diag

    async def _fetch_feed(self) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await request_with_retry(client, "get", ARBEITNOW_ENDPOINT)
            return resp.json()

    @staticmethod
    def _location_eligible(item: dict, search_location: str) -> bool:
        if not search_location:
            return True
        item_loc = item.get("location", "").lower()
        is_remote = item.get("remote", False)
        if search_location.lower() in item_loc:
            return True
        if is_remote:
            return _is_remote_eligible(item.get("location", ""))
        return False


def _parse_arbeitnow_date(val: int | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromtimestamp(val, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None
