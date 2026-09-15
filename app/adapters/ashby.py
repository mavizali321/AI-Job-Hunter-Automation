"""Ashby public job postings adapter."""

import httpx
from datetime import datetime, timezone

from app.adapters.base import SourceAdapter, DiscoveredJob


class AshbyAdapter(SourceAdapter):
    name = "ashby"

    def __init__(self, company_slugs: list[str] | None = None):
        self.company_slugs = company_slugs or []

    async def discover(self, search_terms: list[str], location: str = "", max_results: int = 50) -> list[DiscoveredJob]:
        jobs = []
        async with httpx.AsyncClient(timeout=30) as client:
            for slug in self.company_slugs:
                try:
                    resp = await client.post(
                        "https://jobs.ashbyhq.com/api/non-user-graphql",
                        json={
                            "operationName": "ApiJobBoardWithTeams",
                            "variables": {"organizationHostedJobsPageName": slug},
                            "query": "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) { teams { jobs { id title locationName employmentType } } } }",
                        },
                        timeout=30,
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    board = data.get("data", {}).get("jobBoard", {})
                    for team in board.get("teams", []):
                        for item in team.get("jobs", [])[:max_results]:
                            title = item.get("title", "")
                            terms_match = any(
                                term.lower() in title.lower()
                                for term in search_terms
                            ) if search_terms else True
                            if not terms_match:
                                continue
                            jobs.append(DiscoveredJob(
                                title=title,
                                company=slug,
                                url=f"https://jobs.ashbyhq.com/{slug}/{item.get('id', '')}",
                                source="ashby",
                                external_id=item.get("id", ""),
                                location=item.get("locationName", ""),
                                employment_type=item.get("employmentType", ""),
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
                        title="", company="", url=url, source="ashby",
                        description=resp.text[:5000],
                    )
            except httpx.HTTPError:
                pass
        return None
