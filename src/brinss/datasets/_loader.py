from __future__ import annotations

import os
import time

import pandas as pd

from . import _cache, _catalog, _log, _reading
from ._families import FAMILIES, DatasetFamily
from ._period import PeriodoLike
from .enums import XlsxEngine


def _get_family(name: str) -> DatasetFamily:
    try:
        return FAMILIES[name]
    except KeyError as exc:
        available = ", ".join(sorted(FAMILIES))
        raise KeyError(f"dataset desconhecido: {name!r}. Disponiveis: {available}.") from exc


def load_dataset(
    name: str,
    periodo: PeriodoLike = None,
    *,
    as_dict: bool = False,
    columns: list[str] | None = None,
    force_download: bool = False,
    force_refresh: bool = False,
    cache_dir: str | os.PathLike | None = None,
    engine: XlsxEngine = XlsxEngine.OPENPYXL,
) -> pd.DataFrame | dict[str, pd.DataFrame]:
    """Load one INSS open dataset, downloading and caching files as needed.

    See ``brinss.datasets`` module docs for the full parameter reference.
    """
    family = _get_family(name)
    cache_root = _cache.get_cache_root(cache_dir)
    catalog = _catalog.build_catalog(family, cache_dir=cache_root, force_refresh=force_refresh)
    periods = _catalog.resolve_periods(catalog, periodo)

    frames: dict[str, pd.DataFrame] = {}
    for period in periods:
        entry = catalog.entries_by_period[period]
        path = _cache.fetch_resource(
            entry, family_key=family.key, cache_dir=cache_root, force_download=force_download
        )
        frames[str(period)] = _reading.read_resource(path, entry, columns=columns, engine=engine)

    if as_dict:
        return frames

    started_at = time.perf_counter()
    combined = pd.concat(frames.values(), ignore_index=True)
    if len(frames) > 1:
        # With a single period this would just restate the message read_resource
        # already emitted, so it is only worth logging when there is real work.
        _log.get_logger().info(
            "Concatenated %s periods: %s rows x %s columns in %s.",
            len(frames),
            f"{len(combined):,}",
            len(combined.columns),
            _log.format_seconds(time.perf_counter() - started_at),
        )
    return combined


def list_datasets() -> list[str]:
    return sorted(FAMILIES)


def list_periods(name: str, *, force_refresh: bool = False, cache_dir: str | os.PathLike | None = None) -> list[pd.Period]:
    family = _get_family(name)
    cache_root = _cache.get_cache_root(cache_dir)
    catalog = _catalog.build_catalog(family, cache_dir=cache_root, force_refresh=force_refresh)
    return [entry.period for entry in catalog.entries]
