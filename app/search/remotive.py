"""Remotive public API provider — free, no key required."""

import logging
from datetime import datetime, timezone

import httpx

from app.search.base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

REMOTIVE_ENDPOINT = "https://remotive.com/api/remote-jobs"


class RemotiveProvider(SearchProvider):
    name = "remotive"

    async def search(
        self,
        query: str,
        location: str = "",
        max_results: int = 25,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        params = {"search": query, "limit": min(max_results, 100)}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(REMOTIVE_ENDPOINT, params=params)
            resp.raise_for_status()
            data = resp.json()

        for item in data.get("jobs", [])[:max_results]:
            posted = _parse_remotive_date(item.get("publication_date"))
            loc_candidate = item.get("candidate_required_location", "")
            results.append(SearchResult(
                title=item.get("title", ""),
                company=item.get("company_name", ""),
                location=loc_candidate,
                url=item.get("url", ""),
                snippet=(item.get("description", "") or "")[:500],
                source="remotive",
                posted_date=posted,
                salary=item.get("salary", "") or "",
                remote_policy="remote",
                raw_data=item,
            ))
        return results


def _parse_remotive_date(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(val.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
