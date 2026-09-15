"""Greenhouse public job boards adapter."""

import httpx
from datetime import datetime, timezone

from app.adapters.base import SourceAdapter, DiscoveredJob


class GreenhouseAdapter(SourceAdapter):
    name = "greenhouse"

    def __init__(self, board_tokens: list[str] | None = None):
        self.board_tokens = board_tokens or []

    async def discover(self, search_terms: list[str], location: str = "", max_results: int = 50) -> list[DiscoveredJob]:
        jobs = []
        async with httpx.AsyncClient(timeout=30) as client:
            for token in self.board_tokens:
                try:
                    resp = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    for item in data.get("jobs", [])[:max_results]:
                        title = item.get("title", "")
                        loc = item.get("location", {}).get("name", "")
                        terms_match = any(
                            term.lower() in title.lower() or term.lower() in loc.lower()
                            for term in search_terms
                        ) if search_terms else True
                        if not terms_match:
                            continue
                        jobs.append(DiscoveredJob(
                            title=title,
                            company=token,
                            url=item.get("absolute_url", ""),
                            source="greenhouse",
                            external_id=str(item.get("id", "")),
                            location=loc,
                            posted_date=_parse_gh_date(item.get("updated_at")),
                            raw_data=item,
                        ))
                except httpx.HTTPError:
                    continue
        return jobs

    async def verify(self, url: str) -> DiscoveredJob | None:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code == 200:
                    return DiscoveredJob(
                        title="", company="", url=url, source="greenhouse",
                        description=resp.text[:5000],
                    )
            except httpx.HTTPError:
                pass
        return None


def _parse_gh_date(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
