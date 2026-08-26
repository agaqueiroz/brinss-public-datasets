from __future__ import annotations

import json
import os
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

from . import _ckan, _hf
from ._families import DatasetFamily
from ._period import (
    PeriodoLike,
    normalize_periodo,
    parse_periodo_from_name,
    parse_periodo_from_url,
)
from .enums import DataSource
from .exceptions import (
    BrinssError,
    CkanUnavailableError,
    HuggingFaceUnavailableError,
    PeriodUnavailableError,
)

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
    source: DataSource
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


def _get_metadata(
    key: str,
    fetch: Callable[[], dict],
    *,
    unavailable: type[BrinssError],
    stale_warning: str,
    cache_dir: Path,
    force_refresh: bool,
    ttl_seconds: int,
) -> dict:
    """Fetch a source's metadata, or serve the cached copy while it is young enough.

    A source being down does not have to end the call: a stale cache is handed
    back with a warning, which is what keeps a notebook working through an
    outage on either side. It only raises when there is nothing local to fall
    back to.
    """
    path = _cache_path(cache_dir, key)
    cached = _read_cache(path)

    if not force_refresh and cached is not None and (time.time() - cached["fetched_at"]) < ttl_seconds:
        return cached["result"]

    try:
        result = fetch()
    except unavailable:
        if cached is not None:
            warnings.warn(stale_warning, stacklevel=4)
            return cached["result"]
        raise

    _write_cache(path, result)
    return result


def build_catalog(
    family: DatasetFamily,
    *,
    cache_dir: Path,
    source: DataSource | str,
    force_refresh: bool = False,
    ttl_seconds: int | None = None,
) -> DatasetCatalog:
    """Discover which periods a family has available, from the chosen source.

    Nothing about the period list is hardcoded on either side -- only package
    slugs (in ``_families.py``) and the mirror's path layout (in ``_hf.py``).
    Both sources keep appending a month at a time, so the list is always
    discovered live or from the local metadata cache.
    """
    source = DataSource(source)
    resolved_ttl = ttl_seconds if ttl_seconds is not None else _default_ttl_seconds()
    build = _build_from_manifest if source is DataSource.HF else _build_from_ckan
    entries = build(family, cache_dir=cache_dir, force_refresh=force_refresh, ttl_seconds=resolved_ttl)
    return DatasetCatalog(family=family, source=source, entries=entries)


def _build_from_ckan(
    family: DatasetFamily, *, cache_dir: Path, force_refresh: bool, ttl_seconds: int
) -> tuple[ResourceEntry, ...]:
    """Read the portal's CKAN metadata and turn its resources into entries."""
    entries: dict[pd.Period, ResourceEntry] = {}

    for slug in family.slugs:
        package = _get_metadata(
            slug,
            lambda slug=slug: _ckan.package_show(slug),
            unavailable=CkanUnavailableError,
            stale_warning=f"CKAN indisponivel; usando catalogo em cache (desatualizado) para '{slug}'.",
            cache_dir=cache_dir,
            force_refresh=force_refresh,
            ttl_seconds=ttl_seconds,
        )
        for resource in package.get("resources", []):
            name = resource.get("name", "")
            if not family.matches_resource(name):
                continue  # another dataset sharing this package (see DatasetFamily.resource_filter)

            period = parse_periodo_from_name(name)
            if period is None:
                warnings.warn(
                    f"recurso '{name}' do pacote '{slug}' nao tem periodo reconhecivel no nome, ignorado.",
                    stacklevel=2,
                )
                continue

            if period in entries:
                period = _resolve_period_collision(
                    period, entries=entries, family=family, slug=slug, name=name, url=resource["url"]
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

    return tuple(entries[period] for period in sorted(entries))


def _build_from_manifest(
    family: DatasetFamily, *, cache_dir: Path, force_refresh: bool, ttl_seconds: int
) -> tuple[ResourceEntry, ...]:
    """Read the Hugging Face mirror's manifest and turn its records into entries.

    Much less work than the CKAN side, and not by accident: the manifest is
    keyed by family and period, so there is no resource title to parse a month
    out of, no filter separating three datasets that share one package, and no
    two resources claiming the same month. Those questions were all settled
    once, when the mirror was written.

    The origin's CKAN resource id is carried into the entry so the Parquet
    caches under a name derived from the source file it was built from: when
    the portal replaces a month with a new resource, the mirror's copy of it
    stops matching the cached name too.
    """
    manifest = _get_metadata(
        "hf-manifest",
        _hf.fetch_manifest,
        unavailable=HuggingFaceUnavailableError,
        stale_warning="Hugging Face indisponivel; usando catalogo em cache (desatualizado).",
        cache_dir=cache_dir,
        force_refresh=force_refresh,
        ttl_seconds=ttl_seconds,
    )

    prefix = f"{family.key}/"
    entries: list[ResourceEntry] = []
    for key, record in manifest.get("entries", {}).items():
        if not key.startswith(prefix):
            continue
        period_name = key[len(prefix) :]
        try:
            period = pd.Period(period_name, freq="M")
        except ValueError:
            warnings.warn(
                f"entrada '{key}' do manifesto do Hugging Face nao tem periodo reconhecivel, ignorada.",
                stacklevel=2,
            )
            continue

        entries.append(
            ResourceEntry(
                period=period,
                url=_hf.resolve_url(_hf.path_in_repo(family.key, period_name)),
                resource_id=record.get("resource_id") or "hf",
                # No extension here: _cache._resource_filename takes it from the URL.
                resource_name=f"{family.key}-{period_name}",
                package_slug=_hf.REPO_ID,
                format="PARQUET",
            )
        )

    return tuple(sorted(entries, key=lambda entry: entry.period))


def _resolve_period_collision(
    period: pd.Period,
    *,
    entries: dict[pd.Period, ResourceEntry],
    family: DatasetFamily,
    slug: str,
    name: str,
    url: str,
) -> pd.Period:
    """Settle two resources claiming the same period, and return where the new one goes.

    The portal sometimes mislabels a resource while the file it points to is
    named correctly: there are two "Beneficios Mantidos Suspensos abril 2025",
    and the second one really is ``MANSUSPENSOS.202505``. So the file name is
    asked for a second opinion, on both sides -- the resources come in
    whatever order the package lists them, so either the newcomer or the one
    already seated can be the mislabeled one. The seated one is moved out of
    the way in that case (``entries`` is mutated).
    """
    incumbent = entries[period]
    challenger_period = parse_periodo_from_url(url)
    incumbent_period = parse_periodo_from_url(incumbent.url)

    if challenger_period is not None and challenger_period != period:
        return challenger_period
    if incumbent_period is not None and incumbent_period != period:
        entries[incumbent_period] = replace(entries.pop(period), period=incumbent_period)
        return period

    warnings.warn(
        f"periodo {period} reivindicado por mais de um recurso da familia '{family.key}' "
        f"('{incumbent.resource_name}' de '{incumbent.package_slug}' e '{name}' de '{slug}'), "
        f"e as URLs nao desempatam; mantendo '{name}'.",
        stacklevel=3,
    )
    return period


def _other_source_hint(catalog: DatasetCatalog) -> str:
    """A nudge towards the portal when the mirror simply has not caught up yet.

    The mirror is rebuilt from the portal, so it trails it by up to one
    publication cycle. A month missing from ``hf`` but present on ``inss`` is
    the ordinary state of the first days of a month, not a defect.
    """
    if catalog.source is DataSource.HF:
        return f" A fonte '{DataSource.INSS.value}' pode ter meses mais recentes."
    return ""


def resolve_periods(catalog: DatasetCatalog, periodo: PeriodoLike) -> list[pd.Period]:
    """Turn a user-supplied ``periodo`` into the concrete, available periods to load."""
    if not catalog.entries:
        raise PeriodUnavailableError(
            f"nenhum periodo disponivel para o dataset '{catalog.family.key}' "
            f"na fonte '{catalog.source.value}'.{_other_source_hint(catalog)}"
        )

    normalized = normalize_periodo(periodo)
    available = catalog.entries_by_period

    if normalized is None:
        return [catalog.max_period]

    if normalized == "all":
        return [entry.period for entry in catalog.entries]

    if isinstance(normalized, pd.Period):
        if normalized not in available:
            raise PeriodUnavailableError(
                f"periodo {normalized} indisponivel para '{catalog.family.key}' "
                f"na fonte '{catalog.source.value}'. "
                f"Intervalo disponivel: {catalog.min_period} a {catalog.max_period}."
                f"{_other_source_hint(catalog)}"
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
            f"nenhum dos periodos solicitados esta disponivel para '{catalog.family.key}' "
            f"na fonte '{catalog.source.value}': {requested}. "
            f"Intervalo disponivel: {catalog.min_period} a {catalog.max_period}."
            f"{_other_source_hint(catalog)}"
        )
    missing = [period for period in requested if period not in resolved]
    if missing:
        warnings.warn(
            f"periodos indisponiveis para '{catalog.family.key}' na fonte "
            f"'{catalog.source.value}', ignorados: {missing}.{_other_source_hint(catalog)}",
            stacklevel=3,
        )
