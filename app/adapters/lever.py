"""Lever public job postings adapter."""

import httpx
from datetime import datetime, timezone

from app.adapters.base import SourceAdapter, DiscoveredJob


class LeverAdapter(SourceAdapter):
    name = "lever"

    def __init__(self, company_slugs: list[str] | None = None):
        self.company_slugs = company_slugs or []

    async def discover(self, search_terms: list[str], location: str = "", max_results: int = 50) -> list[DiscoveredJob]:
        jobs = []
        async with httpx.AsyncClient(timeout=30) as client:
            for slug in self.company_slugs:
                try:
                    resp = await client.get(f"https://api.lever.co/v0/postings/{slug}")
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    for item in data[:max_results]:
                        title = item.get("text", "")
                        loc = item.get("categories", {}).get("location", "")
                        terms_match = any(
                            term.lower() in title.lower()
                            for term in search_terms
                        ) if search_terms else True
                        if not terms_match:
                            continue
                        posted_ms = item.get("createdAt")
                        jobs.append(DiscoveredJob(
                            title=title,
                            company=slug,
                            url=item.get("hostedUrl", ""),
                            source="lever",
                            external_id=item.get("id", ""),
                            location=loc,
                            posted_date=datetime.fromtimestamp(posted_ms / 1000, tz=timezone.utc) if posted_ms else None,
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
                        title="", company="", url=url, source="lever",
                        description=resp.text[:5000],
                    )
            except httpx.HTTPError:
                pass
        return None
