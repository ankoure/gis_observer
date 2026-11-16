"""What every harvester shares: the platform registry and error bookkeeping."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import ClassVar

from ..http import HttpGetter
from ..models import Asset, Candidate, Endpoint, Platform


class Harvester(ABC):
    """Reads one endpoint's catalog and yields what it holds.

    Yields `Asset`s for things in this catalog, and `Candidate`s for URLs it points
    at elsewhere (a CKAN resource on an ArcGIS server, an ArcGIS Online item hosted on
    a county server). Candidates go back through discovery, which is how one catalog
    leads to the next.

    `errors` collects transport failures and unreadable responses. A harvest with
    errors is incomplete, so assets missing from it are not marked removed.
    Access-restricted services are not errors: they're recorded as assets.
    """

    platform: ClassVar[Platform]
    _registry: ClassVar[dict[Platform, type[Harvester]]] = {}

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        Harvester._registry[cls.platform] = cls

    def __init__(self, http: HttpGetter) -> None:
        self.http = http
        self.errors: list[str] = []

    @classmethod
    def for_platform(cls, platform: Platform, http: HttpGetter) -> Harvester | None:
        klass = cls._registry.get(platform)
        return klass(http) if klass else None

    @classmethod
    def platforms(cls) -> list[Platform]:
        return list(cls._registry)

    @abstractmethod
    def harvest(self, endpoint: Endpoint) -> AsyncIterator[Asset | Candidate]: ...

    async def get_json(self, url: str, params=None, *, max_bytes=None, quiet: bool = False):
        """Parsed JSON, or None. Failures are recorded unless `quiet`."""
        resp = await self.http.get(url, params=params, max_bytes=max_bytes)
        if resp.error or resp.status is None or resp.status >= 400:
            if not quiet:
                self.errors.append(f"{url}: {resp.error or f'HTTP {resp.status}'}")
            return None
        data = resp.json()
        if data is None and not quiet:
            self.errors.append(f"{url}: response is not JSON")
        return data

    def candidate(self, endpoint: Endpoint, url: str, found_on=None, text=None) -> Candidate:
        return Candidate(url=url, via=f"harvest:{endpoint.id}", found_on=found_on, link_text=text)
