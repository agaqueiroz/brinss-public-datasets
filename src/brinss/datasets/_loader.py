from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from . import _cache, _catalog
from ._families import FAMILIES, DatasetFamily
from ._period import PeriodoLike
from .enums import XlsxEngine
from .exceptions import ColumnNotFoundError


def _get_family(name: str) -> DatasetFamily:
    try:
        return FAMILIES[name]
    except KeyError as exc:
        available = ", ".join(sorted(FAMILIES))
        raise KeyError(f"dataset desconhecido: {name!r}. Disponiveis: {available}.") from exc


def _read_resource(
    path: Path,
    entry: _catalog.ResourceEntry,
    *,
    columns: list[str] | None,
    engine: XlsxEngine,
) -> pd.DataFrame:
    read_kwargs = {"usecols": columns} if columns is not None else {}
    try:
        if entry.format.upper() == "CSV":
            frame = pd.read_csv(path, **read_kwargs)
        else:
            frame = pd.read_excel(path, engine=engine.value, **read_kwargs)
    except ValueError as exc:
        if columns is not None:
            raise ColumnNotFoundError(
                f"coluna solicitada nao encontrada no recurso '{entry.resource_name}' ({entry.period}): {exc}"
            ) from exc
        raise

    frame.insert(0, "periodo_referencia", entry.period)
    return frame


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
        frames[str(period)] = _read_resource(path, entry, columns=columns, engine=engine)

    if as_dict:
        return frames
    return pd.concat(frames.values(), ignore_index=True)


def list_datasets() -> list[str]:
    return sorted(FAMILIES)


def list_periods(name: str, *, force_refresh: bool = False, cache_dir: str | os.PathLike | None = None) -> list[pd.Period]:
    family = _get_family(name)
    cache_root = _cache.get_cache_root(cache_dir)
    catalog = _catalog.build_catalog(family, cache_dir=cache_root, force_refresh=force_refresh)
    return [entry.period for entry in catalog.entries]
