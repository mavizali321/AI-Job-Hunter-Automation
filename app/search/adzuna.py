"""Adzuna job search API provider."""

import logging
from datetime import datetime, timezone

import httpx

from app.search.base import SearchProvider, SearchResult
from app.search.retry import request_with_retry

logger = logging.getLogger(__name__)

ADZUNA_ENDPOINT = "https://api.adzuna.com/v1/api/jobs"


class AdzunaProvider(SearchProvider):
    name = "adzuna"

    def __init__(self, app_id: str, api_key: str, country: str = "pk"):
        self.app_id = app_id
        self.api_key = api_key
        self.country = country

    async def search(
        self,
        query: str,
        location: str = "",
        max_results: int = 25,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        params = {
            "app_id": self.app_id,
            "app_key": self.api_key,
            "what": query,
            "results_per_page": min(max_results, 50),
            "content-type": "application/json",
        }
        if location:
            params["where"] = location

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await request_with_retry(
                client, "get",
                f"{ADZUNA_ENDPOINT}/{self.country}/search/1",
                params=params,
            )
            data = resp.json()

        for item in data.get("results", [])[:max_results]:
            posted = _parse_adzuna_date(item.get("created"))
            results.append(SearchResult(
                title=item.get("title", ""),
                company=item.get("company", {}).get("display_name", ""),
                location=item.get("location", {}).get("display_name", ""),
                url=item.get("redirect_url", ""),
                snippet=item.get("description", "")[:500],
                source="adzuna",
                posted_date=posted,
                salary=_format_salary(item),
                raw_data=item,
            ))
        return results


def _parse_adzuna_date(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _format_salary(item: dict) -> str:
    smin = item.get("salary_min")
    smax = item.get("salary_max")
    if smin and smax:
        return f"{smin}-{smax}"
    if smin:
        return str(smin)
    if smax:
        return str(smax)
    return ""
