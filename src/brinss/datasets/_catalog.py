from __future__ import annotations

import json
import os
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import _ckan
from ._families import DatasetFamily
from ._period import PeriodoLike, normalize_periodo, parse_periodo_from_name
from .exceptions import CkanUnavailableError, PeriodUnavailableError

DEFAULT_CATALOG_TTL_SECONDS = 86400


@dataclass(frozen=True)
class ResourceEntry:
    period: pd.Period
    url: str
    resource_id: str
    resource_name: str
    package_slug: str
    format: str


@dataclass(frozen=True)
class DatasetCatalog:
    family: DatasetFamily
    entries: tuple[ResourceEntry, ...]  # sorted by period, ascending

    @property
    def entries_by_period(self) -> dict[pd.Period, ResourceEntry]:
        return {entry.period: entry for entry in self.entries}

    @property
    def min_period(self) -> pd.Period | None:
        return self.entries[0].period if self.entries else None

    @property
    def max_period(self) -> pd.Period | None:
        return self.entries[-1].period if self.entries else None


def _default_ttl_seconds() -> int:
    value = os.environ.get("BRINSS_CATALOG_TTL_SECONDS")
    if value is None:
        return DEFAULT_CATALOG_TTL_SECONDS
    try:
        return int(value)
    except ValueError:
        return DEFAULT_CATALOG_TTL_SECONDS


def _cache_path(cache_dir: Path, slug: str) -> Path:
    catalog_dir = cache_dir / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    return catalog_dir / f"{slug}.json"


def _read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, result: dict) -> None:
    payload = {"fetched_at": time.time(), "result": result}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _get_package_show(
    slug: str,
    *,
    cache_dir: Path,
    force_refresh: bool,
    ttl_seconds: int,
) -> dict:
    path = _cache_path(cache_dir, slug)
    cached = _read_cache(path)

    if not force_refresh and cached is not None and (time.time() - cached["fetched_at"]) < ttl_seconds:
        return cached["result"]

    try:
        result = _ckan.package_show(slug)
    except CkanUnavailableError:
        if cached is not None:
            warnings.warn(
                f"CKAN indisponivel; usando catalogo em cache (desatualizado) para '{slug}'.",
                stacklevel=3,
            )
            return cached["result"]
        raise

    _write_cache(path, result)
    return result


def build_catalog(
    family: DatasetFamily,
    *,
    cache_dir: Path,
    force_refresh: bool = False,
    ttl_seconds: int | None = None,
) -> DatasetCatalog:
    """Fetch (or reuse the cached) CKAN metadata for a family and build its catalog.

    Only package *slugs* are hardcoded (in ``_families.py``); the list of
    resources -- and therefore of available periods -- is always discovered
    live or from the local metadata cache, since the portal keeps appending
    new monthly resources to the same package over time.
    """
    resolved_ttl = ttl_seconds if ttl_seconds is not None else _default_ttl_seconds()
    entries: dict[pd.Period, ResourceEntry] = {}

    for slug in family.slugs:
        package = _get_package_show(slug, cache_dir=cache_dir, force_refresh=force_refresh, ttl_seconds=resolved_ttl)
        for resource in package.get("resources", []):
            name = resource.get("name", "")
            period = parse_periodo_from_name(name)
            if period is None:
                warnings.warn(
                    f"recurso '{name}' do pacote '{slug}' nao tem periodo reconhecivel no nome, ignorado.",
                    stacklevel=2,
                )
                continue

            if period in entries:
                warnings.warn(
                    f"periodo {period} presente em mais de um pacote da familia '{family.key}' "
                    f"('{entries[period].package_slug}' e '{slug}'); mantendo o de '{slug}'.",
                    stacklevel=2,
                )

            # Later slugs in family.slugs win on overlap (current/rolling package is canonical).
            entries[period] = ResourceEntry(
                period=period,
                url=resource["url"],
                resource_id=resource["id"],
                resource_name=name,
                package_slug=slug,
                format=resource.get("format", ""),
            )

    sorted_entries = tuple(entries[period] for period in sorted(entries))
    return DatasetCatalog(family=family, entries=sorted_entries)


def resolve_periods(catalog: DatasetCatalog, periodo: PeriodoLike) -> list[pd.Period]:
    """Turn a user-supplied ``periodo`` into the concrete, available periods to load."""
    if not catalog.entries:
        raise PeriodUnavailableError(f"nenhum periodo disponivel para o dataset '{catalog.family.key}'.")

    normalized = normalize_periodo(periodo)
    available = catalog.entries_by_period

    if normalized is None:
        return [catalog.max_period]

    if normalized == "all":
        return [entry.period for entry in catalog.entries]

    if isinstance(normalized, pd.Period):
        if normalized not in available:
            raise PeriodUnavailableError(
                f"periodo {normalized} indisponivel para '{catalog.family.key}'. "
                f"Intervalo disponivel: {catalog.min_period} a {catalog.max_period}."
            )
        return [normalized]

    if isinstance(normalized, tuple):
        start, end = normalized
        if start > end:
            start, end = end, start
        requested = list(pd.period_range(start=start, end=end, freq="M"))
    else:
        requested = normalized  # already a list[pd.Period]

    resolved = [period for period in requested if period in available]
    _warn_or_raise_gaps(catalog, requested, resolved)
    return resolved


def _warn_or_raise_gaps(catalog: DatasetCatalog, requested: list[pd.Period], resolved: list[pd.Period]) -> None:
    if not resolved:
        raise PeriodUnavailableError(
            f"nenhum dos periodos solicitados esta disponivel para '{catalog.family.key}': {requested}. "
            f"Intervalo disponivel: {catalog.min_period} a {catalog.max_period}."
        )
    missing = [period for period in requested if period not in resolved]
    if missing:
        warnings.warn(
            f"periodos indisponiveis para '{catalog.family.key}', ignorados: {missing}.",
            stacklevel=3,
        )
