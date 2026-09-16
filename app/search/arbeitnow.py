"""Arbeitnow public API provider — free, no key required."""

import logging
from datetime import datetime, timezone

import httpx

from app.search.base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

ARBEITNOW_ENDPOINT = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowProvider(SearchProvider):
    name = "arbeitnow"

    async def search(
        self,
        query: str,
        location: str = "",
        max_results: int = 25,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        query_lower = query.lower()

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(ARBEITNOW_ENDPOINT)
            resp.raise_for_status()
            data = resp.json()

        for item in data.get("data", []):
            title = item.get("title", "")
            desc = item.get("description", "")
            if query_lower not in title.lower() and query_lower not in desc.lower():
                continue
            posted = _parse_arbeitnow_date(item.get("created_at"))
            loc = item.get("location", "")
            remote = "remote" if item.get("remote", False) else ""
            results.append(SearchResult(
                title=title,
                company=item.get("company_name", ""),
                location=loc,
                url=item.get("url", ""),
                snippet=desc[:500],
                source="arbeitnow",
                posted_date=posted,
                remote_policy=remote,
                raw_data=item,
            ))
            if len(results) >= max_results:
                break
        return results


def _parse_arbeitnow_date(val: int | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromtimestamp(val, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None
