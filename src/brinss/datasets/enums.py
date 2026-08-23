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
