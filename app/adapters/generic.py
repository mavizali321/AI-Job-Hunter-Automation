"""Generic company career page / RSS discovery adapter."""

import httpx
import re
from datetime import datetime

from app.adapters.base import SourceAdapter, DiscoveredJob


class GenericCareerPageAdapter(SourceAdapter):
    name = "generic"

    def __init__(self, career_urls: list[str] | None = None):
        self.career_urls = career_urls or []

    async def discover(self, search_terms: list[str], location: str = "", max_results: int = 50) -> list[DiscoveredJob]:
        jobs = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for url in self.career_urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    content = resp.text[:50000]
                    links = re.findall(r'href=["\']([^"\']*(?:job|career|position|opening)[^"\']*)["\']', content, re.I)
                    for link in links[:max_results]:
                        if not link.startswith("http"):
                            from urllib.parse import urljoin
                            link = urljoin(url, link)
                        jobs.append(DiscoveredJob(
                            title="(to be extracted)",
                            company="(to be extracted)",
                            url=link,
                            source="generic",
                        ))
                except httpx.HTTPError:
                    continue
        return jobs

    async def verify(self, url: str) -> DiscoveredJob | None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return DiscoveredJob(
                        title="", company="", url=url, source="generic",
                        description=resp.text[:5000],
                    )
            except httpx.HTTPError:
                pass
        return None
