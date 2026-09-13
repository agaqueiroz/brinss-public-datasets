class BrinssError(Exception):
    """Base exception for all brinss errors."""


class CkanUnavailableError(BrinssError):
    """The CKAN API could not be reached and no usable local cache was found."""


class HuggingFaceUnavailableError(BrinssError):
    """The Hugging Face mirror could not be reached and no usable local cache was found."""


class PeriodError(BrinssError, ValueError):
    """A ``periodo`` value could not be parsed."""


class PeriodUnavailableError(BrinssError, ValueError):
    """The requested period(s) are not available in the dataset's catalog."""


class ColumnNotFoundError(BrinssError, KeyError):
    """A requested column is missing from one of the dataset's resources."""


class UnsupportedArchiveError(BrinssError):
    """A downloaded ZIP resource doesn't have the expected single-data-file layout."""


class MalformedCsvError(BrinssError):
    """A CSV could not be read without losing columns.

    Raised when the header and the records do not line up. Pandas does not
    complain in that case -- it promotes the leftover columns to an index, which
    writing to Parquet then drops in silence. Published that way, the file loses
    data without anything along the path having failed.
    """
