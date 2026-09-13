from __future__ import annotations

import os
import time
from collections.abc import Generator
from enum import Enum
from pathlib import Path
from typing import Self

import pandas as pd

from . import _cache, _catalog, _log, _reading
from ._catalog import ResourceEntry
from ._families import FAMILIES, DatasetFamily
from ._period import PeriodoLike
from .enums import ColumnDtype, DataSource, XlsxEngine
from .exceptions import StreamEncodingError


class DatasetChunks:
    """The chunks of a ``load_dataset(..., chunksize=N)`` call, one DataFrame at a time.

    Shaped like the reader pandas returns from ``read_csv(chunksize=)``: it is
    an iterator, and also a context manager::

        for chunk in load_dataset("beneficios_emitidos", periodo="all", chunksize=500_000):
            ...

        with load_dataset("beneficios_emitidos", periodo="all", chunksize=500_000) as chunks:
            for chunk in chunks:
                if found:
                    break

    A plain ``for`` that runs to the end leaves nothing open. One that stops
    early leaves the current file open until the garbage collector gets to
    it; the ``with`` block (or ``close()``) releases it on the spot, which on
    Windows is what lets the cached file be deleted or replaced right away.
    """

    def __init__(self, chunks: Generator[pd.DataFrame]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> pd.DataFrame:
        return next(self._chunks)

    def close(self) -> None:
        self._chunks.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


type LoadResult = pd.DataFrame | dict[str, pd.DataFrame] | DatasetChunks
"""What ``load_dataset`` returns: a DataFrame, a dict of them with ``as_dict``, or chunks with ``chunksize``."""


def _get_family(name: str) -> DatasetFamily:
    try:
        return FAMILIES[name]
    except KeyError as exc:
        available = ", ".join(sorted(FAMILIES))
        raise KeyError(f"dataset desconhecido: {name!r}. Disponiveis: {available}.") from exc


def _coerce[T: Enum](enum: type[T], value: T | str, *, label: str) -> T:
    """Validate one enum-valued argument up front, before any (possibly multi-GB) download."""
    try:
        return enum(value)
    except ValueError as exc:
        accepted = ", ".join(repr(member.value) for member in enum)
        raise ValueError(f"{label} invalido: {value!r}. Aceitos: {accepted}.") from exc


def _check_chunksize(chunksize: int | None, *, as_dict: bool) -> None:
    if chunksize is None:
        return
    # bool is an int, and chunksize=True reading one row at a time is surely a mistake.
    if isinstance(chunksize, bool) or not isinstance(chunksize, int) or chunksize < 1:
        raise ValueError(f"chunksize invalido: {chunksize!r}. Use um inteiro positivo (linhas por pedaco).")
    if as_dict:
        raise ValueError("as_dict=True nao combina com chunksize: os pedacos ja saem um periodo por vez.")


def load_dataset(
    name: str,
    periodo: PeriodoLike = None,
    *,
    as_dict: bool = False,
    columns: list[str] | None = None,
    dtype: ColumnDtype | str = ColumnDtype.STRING,
    source: DataSource | str = DataSource.HF,
    force_download: bool = False,
    force_refresh: bool = False,
    cache_dir: str | os.PathLike | None = None,
    engine: XlsxEngine = XlsxEngine.OPENPYXL,
    chunksize: int | None = None,
) -> LoadResult:
    """Load one INSS open dataset, downloading and caching files as needed.

    With ``chunksize`` the data comes back as ``DatasetChunks`` instead of one
    DataFrame: each period is downloaded only when its turn comes, and read at
    most ``chunksize`` rows at a time.

    See ``brinss.datasets`` module docs for the full parameter reference.
    """
    family = _get_family(name)
    dtype = _coerce(ColumnDtype, dtype, label="dtype")
    source = _coerce(DataSource, source, label="source")
    _check_chunksize(chunksize, as_dict=as_dict)
    cache_root = _cache.get_cache_root(cache_dir)
    catalog = _catalog.build_catalog(
        family, cache_dir=cache_root, source=source, force_refresh=force_refresh
    )
    periods = _catalog.resolve_periods(catalog, periodo)

    if chunksize is not None:
        # Everything above runs now, so a bad argument or an unavailable period
        # fails at the call rather than at the first next().
        return DatasetChunks(
            _iter_dataset_chunks(
                [catalog.entries_by_period[period] for period in periods],
                family_key=family.key,
                cache_root=cache_root,
                force_download=force_download,
                columns=columns,
                engine=engine,
                dtype=dtype,
                chunk_rows=chunksize,
            )
        )

    frames: dict[str, pd.DataFrame] = {}
    for period in periods:
        entry = catalog.entries_by_period[period]
        path = _cache.fetch_resource(
            entry, family_key=family.key, cache_dir=cache_root, force_download=force_download
        )
        frames[str(period)] = _reading.read_resource(path, entry, columns=columns, engine=engine, dtype=dtype)

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


def _iter_dataset_chunks(
    entries: list[ResourceEntry],
    *,
    family_key: str,
    cache_root: Path,
    force_download: bool,
    columns: list[str] | None,
    engine: XlsxEngine,
    dtype: ColumnDtype,
    chunk_rows: int,
) -> Generator[pd.DataFrame]:
    """Yield every period's chunks in order, downloading each period only when it is reached."""
    for entry in entries:
        path = _cache.fetch_resource(
            entry, family_key=family_key, cache_dir=cache_root, force_download=force_download
        )
        yield from _stream_resource(path, entry, columns=columns, engine=engine, dtype=dtype, chunk_rows=chunk_rows)


def _stream_resource(
    path: Path,
    entry: ResourceEntry,
    *,
    columns: list[str] | None,
    engine: XlsxEngine,
    dtype: ColumnDtype,
    chunk_rows: int,
) -> Generator[pd.DataFrame]:
    """Yield one resource's chunks, recovering from a wrong encoding while that is still invisible.

    ``open_resource_chunks`` lets a disproved encoding out as a bare
    ``UnicodeDecodeError`` and leaves the fallback to its caller. Here that
    fallback is taken only while no chunk of this file has been handed out: a
    CSV whose first accent sits past the sample, but inside the first chunk,
    just starts over with the next candidate. Past that point the caller
    already holds rows decoded the first way, so the read stops with
    ``StreamEncodingError`` instead. See ``_reading._encoding_candidates``.
    """
    logger = _log.get_logger()
    # Empty for a Parquet or a spreadsheet, where encoding does not apply.
    candidates: list[str | None] = [*_reading.resource_encodings(path)] or [None]
    started_at = time.perf_counter()

    for position, encoding in enumerate(candidates):
        delivered = False
        rows = 0
        width = 0
        try:
            with _reading.open_resource_chunks(
                path, entry, columns=columns, engine=engine, dtype=dtype, encoding=encoding, chunk_rows=chunk_rows
            ) as chunks:
                for chunk in chunks:
                    delivered = True
                    rows += len(chunk)
                    width = len(chunk.columns)
                    yield chunk
        except UnicodeDecodeError as exc:
            if not delivered and position < len(candidates) - 1:
                logger.info(
                    "Encoding '%s' failed before the first chunk; retrying as '%s'.",
                    encoding,
                    candidates[position + 1],
                )
                continue
            raise StreamEncodingError(
                f"'{path.name}' ({entry.period}) nao e '{encoding}' do comeco ao fim: a leitura em "
                f"pedacos ja tinha entregue {rows:,} linhas quando o encoding falhou, e trocar agora "
                "misturaria linhas decodificadas de dois jeitos. Use source=\"hf\", que le Parquet e "
                "nao tem esse problema, ou load_dataset sem chunksize, que refaz a leitura sozinho."
            ) from exc

        logger.info(
            "Streamed %s rows x %s columns from '%s' in %s.",
            f"{rows:,}",
            width,
            path.name,
            _log.format_seconds(time.perf_counter() - started_at),
        )
        return


def list_datasets() -> list[str]:
    return sorted(FAMILIES)


def list_periods(
    name: str,
    *,
    source: DataSource | str = DataSource.HF,
    force_refresh: bool = False,
    cache_dir: str | os.PathLike | None = None,
) -> list[pd.Period]:
    family = _get_family(name)
    source = _coerce(DataSource, source, label="source")
    cache_root = _cache.get_cache_root(cache_dir)
    catalog = _catalog.build_catalog(
        family, cache_dir=cache_root, source=source, force_refresh=force_refresh
    )
    return [entry.period for entry in catalog.entries]
