from __future__ import annotations

import pandas as pd
import pytest

from brinss.datasets._period import (
    normalize_periodo,
    parse_periodo_from_name,
    parse_periodo_from_url,
)
from brinss.datasets.exceptions import PeriodError


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Benefícios concedidos junho 2023", "2023-06"),
        ("Benefícios concedidos Julho 2026", "2026-07"),
        ("beneficios mantidos DEZ 2024", "2024-12"),
        ("CAT jan/2022", "2022-01"),
        (" Benefícios Concedidos fevereiro 2019", "2019-02"),
    ],
)
def test_parse_periodo_from_name_matches(name, expected):
    assert parse_periodo_from_name(name) == pd.Period(expected, freq="M")


@pytest.mark.parametrize(
    "name",
    [
        "Glossário de campos dos relatórios de pessoal",
        "Perfil das unidades - documento de referência",
        "",
    ],
)
def test_parse_periodo_from_name_no_match_returns_none(name):
    assert parse_periodo_from_name(name) is None


def test_normalize_periodo_none():
    assert normalize_periodo(None) is None


def test_normalize_periodo_all():
    assert normalize_periodo("all") == "all"


def test_normalize_periodo_single_string():
    assert normalize_periodo("2024-06") == pd.Period("2024-06", freq="M")


def test_normalize_periodo_range_tuple():
    start, end = normalize_periodo(("2024-01", "2024-03"))
    assert start == pd.Period("2024-01", freq="M")
    assert end == pd.Period("2024-03", freq="M")


def test_normalize_periodo_list():
    result = normalize_periodo(["2024-01", "2024-03"])
    assert result == [pd.Period("2024-01", freq="M"), pd.Period("2024-03", freq="M")]


def test_normalize_periodo_invalid_string_raises():
    with pytest.raises(PeriodError):
        normalize_periodo("nao-e-um-periodo")


def test_normalize_periodo_bad_tuple_length_raises():
    with pytest.raises(PeriodError):
        normalize_periodo(("2024-01", "2024-02", "2024-03"))


def test_normalize_periodo_invalid_type_raises():
    with pytest.raises(PeriodError):
        normalize_periodo(123)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://f.test/x/D.SDA.PDA.004.MANSUSPENSOS.202505.CSV.ZIP", "2025-05"),
        ("https://f.test/x/D.SDA.PDA.004.MANATIVOS.202605.CVS.ZIP", "2026-05"),
        ("https://f.test/Benef%C3%ADcios+mantidos/D.SDA.PDA.005.CAT.202312.csv", "2023-12"),
    ],
)
def test_parse_periodo_from_url_matches(url, expected):
    assert parse_periodo_from_url(url) == pd.Period(expected, freq="M")


@pytest.mark.parametrize(
    "url",
    [
        "https://f.test/x/CONCEDIDOS-DADOS+ABERTOS_JULHO+2026.xlsx",  # month spelled out, no stamp
        "https://f.test/x/agencias-da-previdencia-social-072026.xlsx",  # MMYYYY, not YYYYMM
        "https://f.test/x/relatorio-202513.csv",  # month 13 is not a month
        "https://f.test/x/id-1202505123.csv",  # digits on both sides: not a standalone stamp
        "https://f.test/x/",
    ],
)
def test_parse_periodo_from_url_no_match_returns_none(url):
    assert parse_periodo_from_url(url) is None
