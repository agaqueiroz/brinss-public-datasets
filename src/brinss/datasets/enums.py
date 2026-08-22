from enum import Enum


class XlsxEngine(str, Enum):
    """Engines supported for reading the portal's spreadsheet files."""

    OPENPYXL = "openpyxl"
