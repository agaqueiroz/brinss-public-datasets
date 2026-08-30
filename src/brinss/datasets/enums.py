from enum import Enum


class XlsxEngine(str, Enum):
    """Engines supported for reading the portal's spreadsheet files."""

    OPENPYXL = "openpyxl"


class ColumnDtype(str, Enum):
    """How to type the columns read from the portal's files.

    ``STRING`` is the default because the published files are text: codes like
    CID, CBO, CNAE and the IBGE municipality code carry leading zeros that
    type inference silently destroys ("01234" -> 1234). It also keeps a
    column's dtype stable across months, which matters when several periods
    are concatenated into one frame.
    """

    STRING = "str"
    INFER = "infer"


class DataSource(str, Enum):
    """Where the monthly files are downloaded from.

    ``HF`` is the default. The Hugging Face mirror holds the very same tables
    -- it is built by the companion publisher tool, which reads the portal's
    files through this library and writes one zstd Parquet per month -- but it
    is a far cheaper thing to read: a fraction of
    the bytes over the wire, seconds instead of the minutes openpyxl spends on
    a large sheet, and ``columns`` pushed down into the file so the columns
    nobody asked for are never even decompressed. The banner rows, zip members
    and encoding guesses are already resolved there, once, instead of on every
    read.

    ``INSS`` goes straight to dadosabertos.inss.gov.br. It is the source of
    record, and the one to use for a month that has just been published there
    and has not reached the mirror yet, or to audit the mirror against its
    origin.
    """

    HF = "hf"
    INSS = "inss"
