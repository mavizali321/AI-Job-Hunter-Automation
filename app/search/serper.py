"""Serper.dev Google Search API provider."""

import logging
from datetime import datetime, timezone

import httpx

from app.search.base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

SERPER_ENDPOINT = "https://google.serper.dev/search"


class SerperProvider(SearchProvider):
    name = "serper"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(
        self,
        query: str,
        location: str = "",
        max_results: int = 25,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        params: dict = {
            "q": query,
            "num": min(max_results, 100),
        }
        if location:
            params["gl"] = "pk"
            params["location"] = location

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                SERPER_ENDPOINT,
                json=params,
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        for item in data.get("organic", [])[:max_results]:
            posted = _parse_serper_date(item.get("date"))
            results.append(SearchResult(
                title=item.get("title", ""),
                company=_extract_company_from_serper(item),
                location=location,
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                source="serper",
                posted_date=posted,
                raw_data=item,
            ))
        return results


def _extract_company_from_serper(item: dict) -> str:
    displayed = item.get("sitelinks", [])
    domain = item.get("displayedLink", "") or item.get("link", "")
    from urllib.parse import urlparse
    try:
        host = urlparse(domain if "://" in domain else f"https://{domain}").netloc
        parts = host.replace("www.", "").split(".")
        if parts:
            return parts[0].capitalize()
    except Exception:
        pass
    return ""


def _parse_serper_date(val: str | None) -> datetime | None:
    if not val:
        return None
    import re
    m = re.search(r"(\d+)\s+day", val, re.I)
    if m:
        from datetime import timedelta
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s+hour", val, re.I)
    if m:
        from datetime import timedelta
        return datetime.now(timezone.utc) - timedelta(hours=int(m.group(1)))
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(val.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
