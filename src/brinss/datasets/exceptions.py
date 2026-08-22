class BrinssError(Exception):
    """Base exception for all brinss errors."""


class CkanUnavailableError(BrinssError):
    """The CKAN API could not be reached and no usable local cache was found."""


class PeriodError(BrinssError, ValueError):
    """A ``periodo`` value could not be parsed."""


class PeriodUnavailableError(BrinssError, ValueError):
    """The requested period(s) are not available in the dataset's catalog."""


class ColumnNotFoundError(BrinssError, KeyError):
    """A requested column is missing from one of the dataset's resources."""
