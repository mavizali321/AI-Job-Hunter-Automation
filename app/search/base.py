"""SearchProvider interface — normalized result fields for web job search APIs."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    title: str
    company: str
    location: str
    url: str
    snippet: str
    source: str
    posted_date: datetime | None = None
    salary: str = ""
    remote_policy: str = ""
    raw_data: dict = field(default_factory=dict)


@dataclass
class ProviderDiagnostics:
    provider: str
    requests_made: int = 0
    results_received: int = 0
    results_accepted: int = 0
    errors: list[str] = field(default_factory=list)
    status: str = "ok"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "requests_made": self.requests_made,
            "results_received": self.results_received,
            "results_accepted": self.results_accepted,
            "errors": self.errors,
            "status": self.status,
        }


class SearchProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def search(
        self,
        query: str,
        location: str = "",
        max_results: int = 25,
    ) -> list[SearchResult]:
        ...

    async def search_batch(
        self,
        queries: list[tuple[str, str]],
        max_results: int = 100,
    ) -> tuple[list[SearchResult], ProviderDiagnostics]:
        from app.search.retry import format_provider_error

        diag = ProviderDiagnostics(provider=self.name)
        all_results: list[SearchResult] = []
        max_per_query = max(1, max_results // max(len(queries), 1))

        for query, location in queries:
            if len(all_results) >= max_results:
                break
            diag.requests_made += 1
            try:
                results = await self.search(
                    query, location=location, max_results=max_per_query,
                )
                diag.results_received += len(results)
                all_results.extend(results)
            except Exception as e:
                detail = format_provider_error(e)
                diag.errors.append(f"Query '{query}': {detail}")
                logger.warning(
                    "Provider %s query '%s' failed: %s", self.name, query, detail,
                )

        diag.results_accepted = min(len(all_results), max_results)
        all_results = all_results[:max_results]

        if diag.errors and not all_results:
            diag.status = "failed"
        elif diag.errors:
            diag.status = "partial"
        return all_results, diag
