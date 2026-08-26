from __future__ import annotations

import codecs
import contextlib
import csv
import io
import time
import zipfile
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import pandas as pd

from . import _log
from ._catalog import ResourceEntry
from .enums import ColumnDtype, XlsxEngine
from .exceptions import ColumnNotFoundError, UnsupportedArchiveError

_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE2/Compound File signature (legacy .xls)
_PARQUET_MAGIC = b"PAR1"  # opens and closes every Parquet file
_PERIOD_COLUMN = "periodo_referencia"
_SAMPLE_SIZE = 65536
_ENCODING_SAMPLE_SIZE = 1_000_000
_CHUNK_ROWS = 500_000
_DECODE_CHUNK_SIZE = 1 << 20
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
_HEADER_SCAN_ROWS = 10
_TABULAR_SUFFIXES = (".csv", ".xlsx", ".xlsm", ".xls")


@dataclass(frozen=True)
class _CsvSource:
    """A CSV that can be reopened from byte zero as many times as needed.

    ``open_stream`` is a factory rather than an open handle because the read
    takes more than one pass over the bytes: one bounded pass to sniff the
    dialect, then the pass that parses, then possibly one more if the sampled
    encoding turns out to be wrong. For a zip member each call hands back an
    independent ``ZipExtFile``.
    """

    open_stream: Callable[[], BinaryIO]


@dataclass(frozen=True)
class _ExcelSource:
    """A spreadsheet, which pandas can only read whole."""

    source: Path | io.BytesIO
    engine_name: str


@dataclass(frozen=True)
class _ParquetSource:
    """A Parquet file from the Hugging Face mirror, read whole but column by column."""

    source: Path


def read_resource(
    path: Path,
    entry: ResourceEntry,
    *,
    columns: list[str] | None,
    engine: XlsxEngine,
    dtype: ColumnDtype | str = ColumnDtype.STRING,
) -> pd.DataFrame:
    """Read one downloaded resource into a DataFrame, sniffing its real content.

    Nothing here asks which source the file came from, and neither does the
    caller have to say: a Parquet from the Hugging Face mirror and an XLSX
    from the portal are told apart by their first bytes, like every other
    format. That is also the only honest option on the portal side, where the
    CKAN ``format`` field is not trustworthy: resources labeled "CSV" are
    often actually a ZIP wrapping a single CSV member, and some labeled
    "XLS"/"XLSX" are legacy OLE2 binaries that ``openpyxl`` cannot open.

    The spreadsheets are not laid out predictably either: the "beneficios"
    ones open with a one-cell banner row above the real column names, so the
    header row is located by looking at the sheet rather than assumed to be
    the first one. See ``_excel_header_row``.

    ``dtype`` picks between reading every column as ``str`` (the default,
    faithful to the published text) and letting pandas infer types. Either
    way the ``periodo_referencia`` column added below stays a ``pd.Period``:
    it is the library's own metadata, not a column of the file. The mirror
    stores it as text and it is dropped in ``_read_parquet``, so that both
    sources hand back exactly the same column, of the same type, in the same
    position.

    The whole file lands in one DataFrame, so this is for resources that fit
    in memory. The published "beneficios_mantidos_*" and "beneficios_emitidos"
    months do not -- one of those CSVs runs to 25 GiB and 86 million rows.
    Converting those is what ``open_resource_chunks`` is for.
    """
    dtype = ColumnDtype(dtype)
    logger = _log.get_logger()
    logger.info("Reading '%s' (%s) into a DataFrame...", path.name, _log.format_bytes(path.stat().st_size))
    started_at = time.perf_counter()

    try:
        with _open_source(path, engine=engine) as source:
            if isinstance(source, _CsvSource):
                frame = _read_csv_stream(source.open_stream, columns=columns, dtype=dtype)
            else:
                frame = _read_whole(source, columns=columns, dtype=dtype)
    except ColumnNotFoundError as exc:
        raise ColumnNotFoundError(
            f"coluna solicitada nao encontrada no recurso '{entry.resource_name}' ({entry.period}): {exc}"
        ) from exc

    frame.insert(0, _PERIOD_COLUMN, entry.period)

    logger.info(
        "DataFrame loaded: %s rows x %s columns from '%s' in %s.",
        f"{len(frame):,}",
        len(frame.columns),
        path.name,
        _log.format_seconds(time.perf_counter() - started_at),
    )
    return frame


@contextlib.contextmanager
def open_resource_chunks(
    path: Path,
    entry: ResourceEntry,
    *,
    columns: list[str] | None,
    engine: XlsxEngine,
    dtype: ColumnDtype | str = ColumnDtype.STRING,
    encoding: str | None = None,
    chunk_rows: int = _CHUNK_ROWS,
) -> Generator[Iterator[pd.DataFrame]]:
    """Stream one resource as a sequence of DataFrames, never holding it whole.

    Same reading rules as ``read_resource`` -- same format sniffing, same
    member picking, same ``periodo_referencia`` column, except that it is
    added to every chunk rather than once at the end.

    This is a context manager because the chunks are read lazily out of a live
    ``zipfile.ZipFile``, which has to stay open until the last one is read::

        with open_resource_chunks(path, entry, columns=None, engine=engine) as chunks:
            for chunk in chunks:
                ...

    Leaving the ``with`` closes the archive whether the caller drained the
    chunks, stopped early, or raised. CPython happens to keep the underlying
    file alive through an open member stream even after ``ZipFile.close()`` --
    it refcounts the handle -- but that is undocumented and leaves the
    descriptor dangling until garbage collection, so the lifetime is made
    explicit here rather than relying on it.

    ``encoding`` overrides the one sniffed from the sample. It is for the
    caller that has to restart a conversion after the first choice was
    disproved mid-read; see ``resource_encodings``.

    A resource that pandas can only read whole -- a spreadsheet, or a Parquet
    from the mirror -- yields exactly one chunk, so a caller written against
    this needs only the one code path.
    """
    dtype = ColumnDtype(dtype)
    logger = _log.get_logger()

    with _open_source(path, engine=engine) as source:
        if not isinstance(source, _CsvSource):
            logger.info(
                "Reading '%s' (%s) as a single chunk (%s)...",
                path.name,
                _log.format_bytes(path.stat().st_size),
                "parquet" if isinstance(source, _ParquetSource) else "spreadsheet",
            )
            frame = _read_whole(source, columns=columns, dtype=dtype)
            frame.insert(0, _PERIOD_COLUMN, entry.period)
            yield iter((frame,))
            return

        logger.info(
            "Streaming '%s' (%s) in chunks of %s rows...",
            path.name,
            _log.format_bytes(path.stat().st_size),
            f"{chunk_rows:,}",
        )
        chunks = _iter_csv_chunks(
            source.open_stream,
            entry,
            columns=columns,
            dtype=dtype,
            encoding=encoding,
            chunk_rows=chunk_rows,
        )
        try:
            yield chunks
        finally:
            # Closing the generator matters, and closing the archive is not
            # enough on its own: a caller that stopped early leaves this
            # suspended inside its ``with open_stream()``, and CPython's
            # refcount on the member stream then keeps the archive's descriptor
            # open despite ZipFile.close(). On Windows that is enough to make
            # deleting the file fail. This unwinds it at a defined moment
            # instead of whenever the collector gets there.
            chunks.close()


def resource_encodings(path: Path) -> list[str]:
    """The encodings to try for ``path``, best first; empty for a spreadsheet.

    Only the first one is normally used. The rest matter to a caller that
    streams: a chunked read has already handed rows to its consumer by the
    time a wrong guess blows up, so it cannot quietly retry the way the
    whole-file read does. Such a caller drives the fallback itself, restarting
    the work with the next entry of this list. See ``_encoding_candidates``
    for why a wrong guess always raises instead of mangling accents.
    """
    with _open_source(path, engine=XlsxEngine.OPENPYXL) as source:
        if not isinstance(source, _CsvSource):
            return []
        return _encoding_candidates(*_read_sample(source.open_stream))


@contextlib.contextmanager
def _open_source(
    path: Path, *, engine: XlsxEngine
) -> Generator[_CsvSource | _ExcelSource | _ParquetSource]:
    """Resolve what ``path`` actually holds, keeping any archive open meanwhile."""
    if _starts_with(path, _PARQUET_MAGIC):
        # Checked first, and cheaply: Parquet carries its own encoding and
        # column names, so none of the sniffing below has anything to add.
        yield _ParquetSource(path)
        return

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]

            if any(name == "[Content_Types].xml" or name.startswith("xl/") for name in names):
                # The zip itself IS a genuine XLSX/OOXML spreadsheet; let openpyxl open it directly.
                yield _ExcelSource(path, engine.value)
                return

            data_members = [name for name in names if not name.startswith("__MACOSX")]
            member = _pick_data_member(data_members, archive_name=path.name)
            suffix = PurePosixPath(member).suffix.lower()

            # A spreadsheet has to be materialized: openpyxl and xlrd both seek
            # all over it. Only CSV members are streamed, and those are the ones
            # that run to tens of GB.
            if suffix in (".xlsx", ".xlsm"):
                yield _ExcelSource(io.BytesIO(archive.read(member)), engine.value)
            elif suffix == ".xls":
                yield _ExcelSource(io.BytesIO(archive.read(member)), "xlrd")
            else:
                yield _CsvSource(lambda: archive.open(member))
            return

    if _starts_with(path, _XLS_MAGIC):
        yield _ExcelSource(path, "xlrd")
        return

    yield _CsvSource(lambda: path.open("rb"))


def _starts_with(path: Path, magic: bytes) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(magic)) == magic


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
                if PurePosixPath(name).suffix.lower() == suffix:
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
        source.seek(0)  # a BytesIO from _open_source is left at EOF by the peek

    for index in range(len(preview)):
        if preview.iloc[index].notna().sum() > 1:
            return index
    return 0


def _read_whole(
    source: _ExcelSource | _ParquetSource, *, columns: list[str] | None, dtype: ColumnDtype
) -> pd.DataFrame:
    """Read a source pandas cannot stream, whichever of the two it is."""
    if isinstance(source, _ParquetSource):
        return _read_parquet(source.source, columns=columns, dtype=dtype)
    return _read_excel(source.source, columns=columns, engine_name=source.engine_name, dtype=dtype)


def _read_parquet(path: Path, *, columns: list[str] | None, dtype: ColumnDtype) -> pd.DataFrame:
    """Read one Parquet file from the Hugging Face mirror.

    ``columns`` goes to the reader rather than being applied to the result,
    and that is the one place where the mirror beats the portal by more than
    a constant factor: a column nobody asked for is never decompressed, and on
    the wide families that is most of the file.

    The mirror stores ``periodo_referencia`` as a "YYYY-MM" string -- Parquet
    would otherwise carry the pandas Period as an extension type over the
    month's ordinal (2024-06 becomes 653), which every reader that is not
    pandas would show as that integer. It is dropped here so ``read_resource``
    can insert the real ``pd.Period`` back, exactly as it does for the portal.
    """
    try:
        frame = pd.read_parquet(path, columns=columns)
    except (ValueError, KeyError) as exc:
        # pyarrow reports a missing column as ArrowInvalid, a ValueError.
        if columns is not None:
            raise ColumnNotFoundError(str(exc)) from exc
        raise

    frame = frame.drop(columns=[_PERIOD_COLUMN], errors="ignore")
    return _infer_types(frame) if dtype is ColumnDtype.INFER else frame


def _infer_types(frame: pd.DataFrame) -> pd.DataFrame:
    """Approximate ``read_csv``'s inference over columns that were read as text.

    The mirror's files are text end to end -- that is the whole point of the
    default dtype -- so ``dtype="infer"`` has nothing to hook into while
    reading, the way it does on a CSV. Each column is converted afterwards
    instead, and one that does not convert whole stays as it is.

    On these datasets, whose columns are either numbers or free text, that
    lands on the same result as the portal path. It also drops the leading
    zeros of CID/CBO/CNAE just like reading the CSV with inference would --
    that loss is what ``dtype="infer"`` means; see ``ColumnDtype``.

    Columns are addressed by position, not by name: several families publish
    duplicate column names in code/description pairs, which pandas hands back
    as ``APS`` and ``APS.1`` from a CSV but which arrive from Parquet exactly
    as the source spelled them.
    """
    frame = frame.copy()
    for position in range(len(frame.columns)):
        try:
            frame.isetitem(position, pd.to_numeric(frame.iloc[:, position]))
        except (ValueError, TypeError):
            continue  # genuinely text, and it stays text
    return frame


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


def _read_sample(open_stream: Callable[[], BinaryIO]) -> tuple[bytes, bool]:
    """Read the leading sample used to sniff encoding and delimiter.

    One extra byte is asked for so that a file exactly the size of the sample
    is not mistaken for a truncated one -- the difference decides whether a
    dangling multi-byte sequence at the end is a decoding error or just the cut.
    """
    with open_stream() as handle:
        sample = handle.read(_ENCODING_SAMPLE_SIZE + 1)
    truncated = len(sample) > _ENCODING_SAMPLE_SIZE
    return sample[:_ENCODING_SAMPLE_SIZE], truncated


def _csv_dialect(open_stream: Callable[[], BinaryIO], encoding: str | None) -> tuple[list[str], str]:
    """Return the encodings worth trying and the delimiter, from one sample."""
    sample, truncated = _read_sample(open_stream)
    encodings = [encoding] if encoding is not None else _encoding_candidates(sample, truncated)
    # The cut may land inside a character; errors="replace" is harmless here,
    # since the sniffer only ever looks at the delimiter candidates.
    delimiter = _detect_delimiter(sample[:_SAMPLE_SIZE].decode(encodings[0], errors="replace"))
    return encodings, delimiter


def _read_csv_stream(
    open_stream: Callable[[], BinaryIO], *, columns: list[str] | None, dtype: ColumnDtype
) -> pd.DataFrame:
    """Read a CSV whole, retrying if the sampled encoding is disproved.

    Nothing has been handed to anyone yet when the read blows up, so the
    fallback is invisible from the outside -- unlike the chunked path, which
    has to be restarted by its caller.
    """
    encodings, delimiter = _csv_dialect(open_stream, None)
    read_kwargs = _pandas_read_kwargs(columns, dtype)

    for position, encoding in enumerate(encodings):
        try:
            with open_stream() as handle:
                return pd.read_csv(handle, sep=delimiter, encoding=encoding, **read_kwargs)
        except UnicodeDecodeError:
            # Caught before the ValueError below on purpose: UnicodeDecodeError
            # is one, and reporting it as a missing column would bury the cause.
            if position == len(encodings) - 1:
                raise
            _log.get_logger().info(
                "Encoding '%s' failed past the sample; retrying as '%s'.",
                encoding,
                encodings[position + 1],
            )
        except ValueError as exc:
            if columns is not None:
                raise ColumnNotFoundError(str(exc)) from exc
            raise

    raise AssertionError("unreachable: latin-1 closes the candidate list")


def _iter_csv_chunks(
    open_stream: Callable[[], BinaryIO],
    entry: ResourceEntry,
    *,
    columns: list[str] | None,
    dtype: ColumnDtype,
    encoding: str | None,
    chunk_rows: int,
) -> Iterator[pd.DataFrame]:
    """Yield the CSV a chunk at a time, with the period column on each one."""
    encodings, delimiter = _csv_dialect(open_stream, encoding)
    read_kwargs = _pandas_read_kwargs(columns, dtype)

    with open_stream() as handle:
        try:
            reader = pd.read_csv(
                handle, sep=delimiter, encoding=encodings[0], chunksize=chunk_rows, **read_kwargs
            )
            for chunk in reader:
                chunk.insert(0, _PERIOD_COLUMN, entry.period)
                yield chunk
        except UnicodeDecodeError:
            # Let it out untouched, ahead of the ValueError clause below: the
            # caller restarts the conversion with the next candidate encoding.
            raise
        except ValueError as exc:
            if columns is not None:
                raise ColumnNotFoundError(str(exc)) from exc
            raise


def _detect_encoding(raw: bytes, *, truncated: bool = False) -> str:
    """The encoding to read this payload with: the best of the candidates."""
    return _encoding_candidates(raw, truncated)[0]


def _encoding_candidates(raw: bytes, truncated: bool) -> list[str]:
    """The encodings worth trying for this payload, best first.

    The portal publishes its CSVs as UTF-8 or as Windows-1252 -- the default
    of the tools that export them -- and as nothing else, so trying to decode
    the bytes in that order of preference answers the question exactly.

    ``raw`` is a bounded sample (see ``_ENCODING_SAMPLE_SIZE``), not the whole
    file, because the whole file can be 25 GiB. Bounding it does NOT bring back
    the mangled accents that statistical detection used to cause. That failure
    came from guessing among dozens of charsets: on short Portuguese text the
    guess was "cp1250", where 0xCA is not "E-circumflex" but "E-ogonek", and
    nothing raised while every accent came out wrong. What is left here is
    decoding against a fixed list of three, which fails safe in every direction:

    * the sample fails UTF-8 -- then the file is not UTF-8, and cp1252/latin-1
      is right;
    * the sample decodes as UTF-8 and so does the rest -- right;
    * the sample is plain ASCII but the rest is cp1252 -- ``utf-8-sig`` is
      picked and the read raises ``UnicodeDecodeError`` on the first accented
      byte. Loud, and the next candidate takes over. This is the
      ``perfil_unidades`` shape: it opens with ASCII unit names, so its first
      accent can sit far into the file.

    A wrong pick is therefore always an exception, never a silent mangling.
    ``latin-1`` closes the list: it maps all 256 bytes, so it cannot fail.

    ``truncated`` says the sample was cut at the limit rather than at end of
    file, in which case a dangling multi-byte sequence is the cut's doing and
    must not count against the encoding.
    """
    if raw.startswith(_UTF16_BOMS):
        return ["utf-16"]  # not published today, but a BOM says so unambiguously

    candidates = []
    # utf-8-sig rather than utf-8 so that a leading BOM is stripped instead of
    # ending up glued to the first column's name.
    if _decodes_fully(raw, "utf-8", final=not truncated):
        candidates.append("utf-8-sig")
    if _decodes_fully(raw, "cp1252", final=not truncated):
        candidates.append("cp1252")
    candidates.append("latin-1")
    return candidates


def _decodes_fully(raw: bytes, encoding: str, *, final: bool = True) -> bool:
    """Whether ``raw`` decodes end to end under ``encoding``.

    A plain ``raw.decode()`` would materialize a second copy of a payload that
    runs to a megabyte just to answer yes or no. The incremental decoder drops
    each chunk as it goes and carries over the multi-byte sequences straddling
    a chunk boundary.

    ``final=False`` leaves the last sequence open, for a sample cut at an
    arbitrary offset: without it a cut landing mid-character would read as a
    broken encoding rather than a broken cut.
    """
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        for start in range(0, len(raw), _DECODE_CHUNK_SIZE):
            decoder.decode(raw[start : start + _DECODE_CHUNK_SIZE])
        if final:
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
