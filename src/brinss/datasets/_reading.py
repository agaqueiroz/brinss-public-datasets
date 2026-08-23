from __future__ import annotations

import codecs
import csv
import io
import time
import zipfile
from pathlib import Path, PurePosixPath

import pandas as pd

from . import _log
from ._catalog import ResourceEntry
from .enums import ColumnDtype, XlsxEngine
from .exceptions import ColumnNotFoundError, UnsupportedArchiveError

_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE2/Compound File signature (legacy .xls)
_SAMPLE_SIZE = 65536
_DECODE_CHUNK_SIZE = 1 << 20
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
_HEADER_SCAN_ROWS = 10
_TABULAR_SUFFIXES = (".csv", ".xlsx", ".xlsm", ".xls")


def read_resource(
    path: Path,
    entry: ResourceEntry,
    *,
    columns: list[str] | None,
    engine: XlsxEngine,
    dtype: ColumnDtype | str = ColumnDtype.STRING,
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

    ``dtype`` picks between reading every column as ``str`` (the default,
    faithful to the published text) and letting pandas infer types. Either
    way the ``periodo_referencia`` column added below stays a ``pd.Period``:
    it is the library's own metadata, not a column of the file.
    """
    dtype = ColumnDtype(dtype)  # a plain "str"/"infer" is == but not `is` a member
    logger = _log.get_logger()
    logger.info("Reading '%s' (%s) into a DataFrame...", path.name, _log.format_bytes(path.stat().st_size))
    started_at = time.perf_counter()

    try:
        if zipfile.is_zipfile(path):
            frame = _read_zip(path, columns=columns, engine=engine, dtype=dtype)
        elif _looks_like_legacy_xls(path):
            frame = _read_excel(path, columns=columns, engine_name="xlrd", dtype=dtype)
        else:
            frame = _read_csv_bytes(path.read_bytes(), columns=columns, dtype=dtype)
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


def _read_zip(path: Path, *, columns: list[str] | None, engine: XlsxEngine, dtype: ColumnDtype) -> pd.DataFrame:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]

        if any(name == "[Content_Types].xml" or name.startswith("xl/") for name in names):
            # The zip itself IS a genuine XLSX/OOXML spreadsheet; let openpyxl open it directly.
            return _read_excel(path, columns=columns, engine_name=engine.value, dtype=dtype)

        data_members = [name for name in names if not name.startswith("__MACOSX")]
        member = _pick_data_member(data_members, archive_name=path.name)
        raw = archive.read(member)

    suffix = Path(member).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return _read_excel(io.BytesIO(raw), columns=columns, engine_name=engine.value, dtype=dtype)
    if suffix == ".xls":
        return _read_excel(io.BytesIO(raw), columns=columns, engine_name="xlrd", dtype=dtype)
    return _read_csv_bytes(raw, columns=columns, dtype=dtype)


def _member_stem(name: str) -> str:
    """The member's path with its extension removed (zip names always use "/")."""
    return str(PurePosixPath(name).with_suffix(""))


def _pick_data_member(names: list[str], *, archive_name: str) -> str:
    """Pick the one member of a zip that holds the data to read.

    A member count above one does not mean the archive is malformed: the
    "comunicacoes_acidente_trabalho" resources ship the SAME dataset three
    times over inside a single zip -- ``D.SDA.PDA.005.CAT.202605.csv``,
    ``.json`` and ``.xml``. Members that differ only by extension are treated
    as one dataset, and the tabular serialization is the one read (csv first,
    then the Excel ones); the others are never decompressed, they just stay
    inside the archive.

    Genuinely different datasets in one zip (different stems) still raise,
    since nothing there says which one the caller meant.
    """
    if len(names) == 1:
        return names[0]

    if len({_member_stem(name) for name in names}) == 1:
        for suffix in _TABULAR_SUFFIXES:
            for name in names:
                if Path(name).suffix.lower() == suffix:
                    _log.get_logger().info(
                        "Zip '%s' holds one dataset in %s formats; reading '%s', ignoring: %s.",
                        archive_name,
                        len(names),
                        name,
                        ", ".join(other for other in names if other != name),
                    )
                    return name

        raise UnsupportedArchiveError(
            f"zip '{archive_name}' nao traz nenhum formato tabular "
            f"({', '.join(_TABULAR_SUFFIXES)}) entre seus membros: {names!r}"
        )

    raise UnsupportedArchiveError(
        f"zip '{archive_name}' tem estrutura inesperada (esperava 1 arquivo de dados, "
        f"encontrado {len(names)}): {names!r}"
    )


def _pandas_read_kwargs(columns: list[str] | None, dtype: ColumnDtype) -> dict:
    """Build the kwargs shared by both ``pd.read_*`` calls.

    ``dtype=str`` still leaves empty cells as ``NaN`` rather than ``""``, so
    ``.isna()`` keeps working the way pandas users expect.
    """
    kwargs: dict = {}
    if columns is not None:
        kwargs["usecols"] = columns
    if dtype is ColumnDtype.STRING:
        kwargs["dtype"] = str
    return kwargs


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


def _read_excel(
    source: Path | io.BytesIO, *, columns: list[str] | None, engine_name: str, dtype: ColumnDtype
) -> pd.DataFrame:
    header = _excel_header_row(source, engine_name=engine_name)
    read_kwargs = _pandas_read_kwargs(columns, dtype)
    try:
        return pd.read_excel(source, engine=engine_name, header=header, **read_kwargs)
    except ValueError as exc:
        if columns is not None:
            raise ColumnNotFoundError(str(exc)) from exc
        raise


def _read_csv_bytes(raw: bytes, *, columns: list[str] | None, dtype: ColumnDtype) -> pd.DataFrame:
    encoding = _detect_encoding(raw)
    # The cut may land inside a character; errors="replace" is harmless here,
    # since the sniffer only ever looks at the delimiter candidates.
    delimiter = _detect_delimiter(raw[:_SAMPLE_SIZE].decode(encoding, errors="replace"))

    read_kwargs = _pandas_read_kwargs(columns, dtype)
    try:
        return pd.read_csv(io.BytesIO(raw), sep=delimiter, encoding=encoding, **read_kwargs)
    except ValueError as exc:
        if columns is not None:
            raise ColumnNotFoundError(str(exc)) from exc
        raise


def _detect_encoding(raw: bytes) -> str:
    """Return the encoding the whole payload decodes cleanly with.

    The portal publishes its CSVs as UTF-8 or as Windows-1252 -- the default
    of the tools that export them -- and as nothing else, so trying to decode
    the bytes in that order of preference answers the question exactly.

    Statistical detection over a leading 64 KB sample used to do this, and it
    was wrong in both directions. On a file whose accents only start thousands
    of rows in -- ``perfil_unidades`` opens with plain ASCII unit names -- it
    answers "ascii", and the read then dies with a ``UnicodeDecodeError`` on
    the first accented byte past the sample. On short Portuguese text it
    answers "cp1250", where 0xCA is not "E-circumflex" but "E-ogonek": no
    error is raised at all and every accent comes out silently mangled.
    Looking at the bytes that will actually be read rules out both.

    ``utf-8-sig`` is returned rather than ``utf-8`` so that a leading BOM is
    stripped instead of ending up glued to the first column's name.
    """
    if raw.startswith(_UTF16_BOMS):
        return "utf-16"  # not published today, but a BOM says so unambiguously
    if _decodes_fully(raw, "utf-8"):
        return "utf-8-sig"
    if _decodes_fully(raw, "cp1252"):
        return "cp1252"
    return "latin-1"  # maps every one of the 256 bytes, so it cannot fail


def _decodes_fully(raw: bytes, encoding: str) -> bool:
    """Whether ``raw`` decodes end to end under ``encoding``.

    A plain ``raw.decode()`` would materialize a second copy of a file that
    can run to tens of megabytes just to answer yes or no. The incremental
    decoder drops each chunk as it goes and carries over the multi-byte
    sequences straddling a chunk boundary, so a truncated one at the very end
    still fails the way it should.
    """
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        for start in range(0, len(raw), _DECODE_CHUNK_SIZE):
            decoder.decode(raw[start : start + _DECODE_CHUNK_SIZE])
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _detect_delimiter(sample_text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=";,\t|")
        return dialect.delimiter
    except csv.Error:
        return ";"
