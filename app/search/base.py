"""SearchProvider interface — normalized result fields for web job search APIs."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


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
