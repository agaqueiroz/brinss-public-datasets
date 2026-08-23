from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Literal, Union

import pandas as pd

from .exceptions import PeriodError

PeriodoLike = Union[None, str, "tuple[str, str]", "list[str]", Literal["all"]]

_MONTHS_PT: dict[str, int] = {
    "janeiro": 1,
    "jan": 1,
    "fevereiro": 2,
    "fev": 2,
    "marco": 3,
    "mar": 3,
    "abril": 4,
    "abr": 4,
    "maio": 5,
    "mai": 5,
    "junho": 6,
    "jun": 6,
    "julho": 7,
    "jul": 7,
    "agosto": 8,
    "ago": 8,
    "setembro": 9,
    "set": 9,
    "outubro": 10,
    "out": 10,
    "novembro": 11,
    "nov": 11,
    "dezembro": 12,
    "dez": 12,
}

_MONTH_ALTERNATION = "|".join(sorted(_MONTHS_PT, key=len, reverse=True))
_MONTH_YEAR_RE = re.compile(rf"(?P<month>{_MONTH_ALTERNATION})\D{{0,3}}(?P<year>(?:19|20)\d{{2}})")
_YYYYMM_RE = re.compile(r"(?<!\d)(?P<year>20\d{2})(?P<month>0[1-9]|1[0-2])(?!\d)")


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def parse_periodo_from_name(name: str) -> pd.Period | None:
    """Extract a monthly ``pd.Period`` from a free-text CKAN resource name.

    Returns ``None`` instead of raising when no recognizable month/year is
    found, since the portal occasionally publishes resources with names that
    don't follow the usual "<mes> <ano>" pattern.
    """
    normalized = _strip_accents(name).lower()
    match = _MONTH_YEAR_RE.search(normalized)
    if match is None:
        return None
    month = _MONTHS_PT[match.group("month")]
    year = int(match.group("year"))
    return pd.Period(year=year, month=month, freq="M")


def parse_periodo_from_url(url: str) -> pd.Period | None:
    """Extract a monthly ``pd.Period`` from the file name a resource points to.

    Most of the portal's file names carry an unambiguous ``YYYYMM`` stamp
    (``D.SDA.PDA.004.MANSUSPENSOS.202505.CSV.ZIP``), which is a second opinion
    on the period when the resource's own name is not to be trusted. Returns
    ``None`` when there is no such stamp -- the "concedidos" and "indeferidos"
    files are named after the month in Portuguese instead.
    """
    filename = urllib.parse.unquote(url).rstrip("/").rsplit("/", 1)[-1]
    match = _YYYYMM_RE.search(filename)
    if match is None:
        return None
    return pd.Period(year=int(match.group("year")), month=int(match.group("month")), freq="M")


def _parse_single(value: str) -> pd.Period:
    try:
        return pd.Period(value, freq="M")
    except (ValueError, TypeError) as exc:
        raise PeriodError(
            f"periodo invalido: {value!r}. Formatos aceitos: 'YYYY-MM' (ex: '2024-06'), "
            "'YYYY-MM-DD', ou qualquer valor aceito por pandas.Period(freq='M')."
        ) from exc


def normalize_periodo(
    value: PeriodoLike,
) -> None | Literal["all"] | pd.Period | tuple[pd.Period, pd.Period] | list[pd.Period]:
    """Validate and normalize a user-supplied ``periodo`` argument.

    This does not check availability against any catalog -- that happens
    later, in ``_catalog.resolve_periods``, the only place that knows what
    periods actually exist for a given dataset family.
    """
    if value is None:
        return None
    if value == "all":
        return "all"
    if isinstance(value, str):
        return _parse_single(value)
    if isinstance(value, tuple):
        if len(value) != 2:
            raise PeriodError(
                "periodo em formato de intervalo deve ter exatamente 2 elementos "
                f"(inicio, fim), recebido: {value!r}"
            )
        start, end = value
        return (_parse_single(start), _parse_single(end))
    if isinstance(value, list):
        return [_parse_single(item) for item in value]
    raise PeriodError(
        "periodo deve ser None, uma string tipo '2024-06', uma tupla (inicio, fim), "
        f"uma lista de strings, ou 'all'. Recebido: {value!r}"
    )
