from __future__ import annotations

import csv
import io
import time
import zipfile
from pathlib import Path

import charset_normalizer
import pandas as pd

from . import _log
from ._catalog import ResourceEntry
from .enums import XlsxEngine
from .exceptions import ColumnNotFoundError, UnsupportedArchiveError

_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE2/Compound File signature (legacy .xls)
_SAMPLE_SIZE = 65536
_HEADER_SCAN_ROWS = 10


def read_resource(
    path: Path,
    entry: ResourceEntry,
    *,
    columns: list[str] | None,
    engine: XlsxEngine,
) -> pd.DataFrame:
    """Read one downloaded resource into a DataFrame, sniffing its real content.

    The CKAN portal's ``format`` field is not trustworthy: resources labeled
    "CSV" are often actually a ZIP wrapping a single CSV member, and some
    resources labeled "XLS"/"XLSX" are legacy OLE2 binaries that ``openpyxl``
    cannot open. The downloaded bytes are inspected instead of trusting that
    metadata.

    The spreadsheets are not laid out predictably either: the "beneficios"
    ones open with a one-cell banner row above the real column names, so the
    header row is located by looking at the sheet rather than assumed to be
    the first one. See ``_excel_header_row``.
    """
    logger = _log.get_logger()
    logger.info("Reading '%s' (%s) into a DataFrame...", path.name, _log.format_bytes(path.stat().st_size))
    started_at = time.perf_counter()

    try:
        if zipfile.is_zipfile(path):
            frame = _read_zip(path, columns=columns, engine=engine)
        elif _looks_like_legacy_xls(path):
            frame = _read_excel(path, columns=columns, engine_name="xlrd")
        else:
            frame = _read_csv_bytes(path.read_bytes(), columns=columns)
    except ColumnNotFoundError as exc:
        raise ColumnNotFoundError(
            f"coluna solicitada nao encontrada no recurso '{entry.resource_name}' ({entry.period}): {exc}"
        ) from exc

    frame.insert(0, "periodo_referencia", entry.period)

    logger.info(
        "DataFrame loaded: %s rows x %s columns from '%s' in %s.",
        f"{len(frame):,}",
        len(frame.columns),
        path.name,
        _log.format_seconds(time.perf_counter() - started_at),
    )
    return frame


def _looks_like_legacy_xls(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(_XLS_MAGIC)) == _XLS_MAGIC


def _read_zip(path: Path, *, columns: list[str] | None, engine: XlsxEngine) -> pd.DataFrame:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]

        if any(name == "[Content_Types].xml" or name.startswith("xl/") for name in names):
            # The zip itself IS a genuine XLSX/OOXML spreadsheet; let openpyxl open it directly.
            return _read_excel(path, columns=columns, engine_name=engine.value)

        data_members = [name for name in names if not name.startswith("__MACOSX")]
        if len(data_members) != 1:
            raise UnsupportedArchiveError(
                f"zip '{path.name}' tem estrutura inesperada (esperava 1 arquivo de dados, "
                f"encontrado {len(data_members)}): {data_members!r}"
            )

        member = data_members[0]
        raw = archive.read(member)

    suffix = Path(member).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return _read_excel(io.BytesIO(raw), columns=columns, engine_name=engine.value)
    if suffix == ".xls":
        return _read_excel(io.BytesIO(raw), columns=columns, engine_name="xlrd")
    return _read_csv_bytes(raw, columns=columns)


def _excel_header_row(source: Path | io.BytesIO, *, engine_name: str) -> int:
    """Return the index of the row holding the real column names.

    ``beneficios_concedidos`` and ``beneficios_indeferidos`` publish sheets
    whose first row is a title banner filling a single cell -- everything
    below it shifts by one, which is what turns the column names into
    ``Unnamed: 1``, ``Unnamed: 2``, and so on. The banner's text is rewritten
    every month ("CONCEDIDOS DADOS ABERTOS - MAIO DE 2026" one month,
    "DADOS ABERTOS - BENEFICIOS CONCEDIDOS - ANO JULHO DE 2026" the next), so
    it is recognized by its shape instead: the header is the first row that
    fills more than one cell. Sheets that start with a proper header, like
    ``perfil_unidades``, land on row 0 and read exactly as before.
    """
    preview = pd.read_excel(source, engine=engine_name, header=None, nrows=_HEADER_SCAN_ROWS)
    if hasattr(source, "seek"):
        source.seek(0)  # a BytesIO from _read_zip is left at EOF by the peek

    for index in range(len(preview)):
        if preview.iloc[index].notna().sum() > 1:
            return index
    return 0


def _read_excel(source: Path | io.BytesIO, *, columns: list[str] | None, engine_name: str) -> pd.DataFrame:
    header = _excel_header_row(source, engine_name=engine_name)
    read_kwargs = {"usecols": columns} if columns is not None else {}
    try:
        return pd.read_excel(source, engine=engine_name, header=header, **read_kwargs)
    except ValueError as exc:
        if columns is not None:
            raise ColumnNotFoundError(str(exc)) from exc
        raise


def _read_csv_bytes(raw: bytes, *, columns: list[str] | None) -> pd.DataFrame:
    sample = raw[:_SAMPLE_SIZE]
    encoding = _detect_encoding(sample)
    delimiter = _detect_delimiter(sample.decode(encoding, errors="replace"))

    read_kwargs = {"usecols": columns} if columns is not None else {}
    try:
        return pd.read_csv(io.BytesIO(raw), sep=delimiter, encoding=encoding, **read_kwargs)
    except ValueError as exc:
        if columns is not None:
            raise ColumnNotFoundError(str(exc)) from exc
        raise


def _detect_encoding(sample: bytes) -> str:
    match = charset_normalizer.from_bytes(sample).best()
    if match is not None and match.encoding:
        return match.encoding
    return "latin-1"


def _detect_delimiter(sample_text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=";,\t|")
        return dialect.delimiter
    except csv.Error:
        return ";"
