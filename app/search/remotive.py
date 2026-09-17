"""Remotive public API provider — free, no key required.

Queries by role only (remote-only jobs, location is irrelevant to the API).
Deduplicates identical role queries so the same role paired with multiple
locations triggers only one HTTP request.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.search.base import SearchProvider, SearchResult, ProviderDiagnostics
from app.search.retry import request_with_retry, format_provider_error

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
            resp = await request_with_retry(
                client, "get", REMOTIVE_ENDPOINT, params=params,
            )
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

    async def search_batch(
        self,
        queries: list[tuple[str, str]],
        max_results: int = 100,
    ) -> tuple[list[SearchResult], ProviderDiagnostics]:
        diag = ProviderDiagnostics(provider=self.name)

        unique_roles: list[str] = []
        seen_roles: set[str] = set()
        for query, _location in queries:
            key = query.strip().lower()
            if key not in seen_roles:
                seen_roles.add(key)
                unique_roles.append(query)

        seen_urls: set[str] = set()
        all_results: list[SearchResult] = []
        max_per_role = max(1, max_results // max(len(unique_roles), 1))

        for role in unique_roles:
            if len(all_results) >= max_results:
                break
            diag.requests_made += 1
            try:
                results = await self.search(role, max_results=max_per_role)
                diag.results_received += len(results)
                for sr in results:
                    if len(all_results) >= max_results:
                        break
                    if sr.url in seen_urls:
                        continue
                    seen_urls.add(sr.url)
                    all_results.append(sr)
            except Exception as e:
                detail = format_provider_error(e)
                diag.errors.append(f"Role '{role}': {detail}")
                logger.warning(
                    "Remotive query '%s' failed: %s", role, detail,
                )

        diag.results_accepted = len(all_results)
        if diag.errors and not all_results:
            diag.status = "failed"
        elif diag.errors:
            diag.status = "partial"
        return all_results, diag


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
