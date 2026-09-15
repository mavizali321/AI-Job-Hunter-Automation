"""Base adapter interface for job discovery sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DiscoveredJob:
    title: str
    company: str
    url: str
    source: str
    external_id: str = ""
    requisition_id: str = ""
    location: str = ""
    remote_policy: str = ""
    description: str = ""
    requirements: str = ""
    salary: str = ""
    posted_date: datetime | None = None
    closing_date: datetime | None = None
    employment_type: str = ""
    raw_data: dict = field(default_factory=dict)


class SourceAdapter(ABC):
    name: str = "base"

    @abstractmethod
    async def discover(self, search_terms: list[str], location: str = "", max_results: int = 50) -> list[DiscoveredJob]:
        ...

    @abstractmethod
    async def verify(self, url: str) -> DiscoveredJob | None:
        ...
