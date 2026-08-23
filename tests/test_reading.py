from __future__ import annotations

import zipfile

import pandas as pd
import pytest

from brinss.datasets._catalog import ResourceEntry
from brinss.datasets._reading import read_resource
from brinss.datasets.enums import XlsxEngine
from brinss.datasets.exceptions import ColumnNotFoundError, UnsupportedArchiveError

ROWS = [
    {"beneficio": "aposentadoria", "valor": 1500},
    {"beneficio": "auxilio", "valor": 900},
]


def _entry(*, resource_name: str = "Beneficios emitidos junho 2024", format_: str = "CSV") -> ResourceEntry:
    return ResourceEntry(
        period=pd.Period("2024-06", freq="M"),
        url="https://fixtures.test/res.bin",
        resource_id="res-1",
        resource_name=resource_name,
        package_slug="slug",
        format=format_,
    )


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_read_resource_raw_csv(tmp_path, make_csv_bytes):
    path = _write(tmp_path, "res.csv", make_csv_bytes(ROWS))
    df = read_resource(path, _entry(), columns=None, engine=XlsxEngine.OPENPYXL)

    assert list(df["periodo_referencia"]) == [pd.Period("2024-06", freq="M")] * 2
    assert df.loc[0, "beneficio"] == "aposentadoria"
    assert df.loc[0, "valor"] == 1500


def test_read_resource_csv_inside_zip_wrapper(tmp_path, make_csv_zip_bytes):
    # Mirrors the real portal pattern: a resource labeled "CSV" that's actually
    # a ZIP wrapping a single ";"-delimited, latin-1 CSV member.
    path = _write(tmp_path, "res.zip", make_csv_zip_bytes(ROWS, member_name="D.SDA.PDA.emitidos.csv"))
    df = read_resource(path, _entry(), columns=None, engine=XlsxEngine.OPENPYXL)

    assert len(df) == 2
    assert set(df["beneficio"]) == {"aposentadoria", "auxilio"}


def test_read_resource_genuine_xlsx(tmp_path, make_xlsx_bytes):
    path = _write(tmp_path, "res.xlsx", make_xlsx_bytes(ROWS))
    df = read_resource(path, _entry(format_="XLSX"), columns=None, engine=XlsxEngine.OPENPYXL)

    assert len(df) == 2
    assert df.loc[1, "beneficio"] == "auxilio"


def test_read_resource_legacy_xls(tmp_path, make_xls_bytes):
    path = _write(tmp_path, "res.xls", make_xls_bytes(ROWS))
    df = read_resource(path, _entry(format_="XLS"), columns=None, engine=XlsxEngine.OPENPYXL)

    assert len(df) == 2
    assert set(df["beneficio"]) == {"aposentadoria", "auxilio"}


def test_read_resource_zip_with_two_data_members_raises(tmp_path, make_csv_zip_bytes):
    data = make_csv_zip_bytes(ROWS, member_name="a.csv", extra_members={"b.csv": b"x;y\r\n1;2\r\n"})
    path = _write(tmp_path, "res.zip", data)

    with pytest.raises(UnsupportedArchiveError):
        read_resource(path, _entry(), columns=None, engine=XlsxEngine.OPENPYXL)


def test_read_resource_zip_with_no_data_members_raises(tmp_path):
    buffer_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(buffer_path, "w") as archive:
        archive.writestr("subdir/", "")

    with pytest.raises(UnsupportedArchiveError):
        read_resource(buffer_path, _entry(), columns=None, engine=XlsxEngine.OPENPYXL)


def test_read_resource_missing_column_raises_for_csv(tmp_path, make_csv_bytes):
    path = _write(tmp_path, "res.csv", make_csv_bytes(ROWS))
    with pytest.raises(ColumnNotFoundError, match="Beneficios emitidos junho 2024"):
        read_resource(path, _entry(), columns=["coluna_inexistente"], engine=XlsxEngine.OPENPYXL)


def test_read_resource_missing_column_raises_for_xlsx(tmp_path, make_xlsx_bytes):
    path = _write(tmp_path, "res.xlsx", make_xlsx_bytes(ROWS))
    with pytest.raises(ColumnNotFoundError, match="Beneficios emitidos junho 2024"):
        read_resource(path, _entry(format_="XLSX"), columns=["coluna_inexistente"], engine=XlsxEngine.OPENPYXL)


# The real "beneficios" spreadsheets open with a one-cell title row above the
# header, which shifts every column and turns their names into "Unnamed: N".
BANNER = "DADOS ABERTOS - BENEFICIOS CONCEDIDOS - ANO JULHO DE 2026"


def test_read_resource_xlsx_skips_banner_row(tmp_path, make_xlsx_bytes):
    path = _write(tmp_path, "res.xlsx", make_xlsx_bytes(ROWS, banner=BANNER))
    df = read_resource(path, _entry(format_="XLSX"), columns=None, engine=XlsxEngine.OPENPYXL)

    assert list(df.columns) == ["periodo_referencia", "beneficio", "valor"]
    assert not [column for column in df.columns if str(column).startswith("Unnamed")]
    assert len(df) == 2  # the banner is dropped, not carried in as a data row
    assert df.loc[0, "beneficio"] == "aposentadoria"


def test_read_resource_xlsx_without_banner_is_unchanged(tmp_path, make_xlsx_bytes):
    # Guards perfil_unidades, whose sheets already start with a proper header.
    path = _write(tmp_path, "res.xlsx", make_xlsx_bytes(ROWS))
    df = read_resource(path, _entry(format_="XLSX"), columns=None, engine=XlsxEngine.OPENPYXL)

    assert list(df.columns) == ["periodo_referencia", "beneficio", "valor"]
    assert len(df) == 2


def test_read_resource_xlsx_duplicate_headers_get_pandas_suffix(tmp_path, make_xlsx_bytes):
    # concedidos repeats names for code/description pairs: APS, APS, Especie, Especie...
    data = make_xlsx_bytes(ROWS, banner=BANNER, headers=["APS", "APS"])
    path = _write(tmp_path, "res.xlsx", data)
    df = read_resource(path, _entry(format_="XLSX"), columns=None, engine=XlsxEngine.OPENPYXL)

    assert list(df.columns) == ["periodo_referencia", "APS", "APS.1"]


def test_read_resource_xlsx_with_banner_accepts_real_column_names(tmp_path, make_xlsx_bytes):
    # Before the header row was detected, columns= could not work on these files
    # at all: the names to ask for were "Unnamed: 1", "Unnamed: 2", ...
    path = _write(tmp_path, "res.xlsx", make_xlsx_bytes(ROWS, banner=BANNER))
    df = read_resource(path, _entry(format_="XLSX"), columns=["beneficio"], engine=XlsxEngine.OPENPYXL)

    assert list(df.columns) == ["periodo_referencia", "beneficio"]
    assert list(df["beneficio"]) == ["aposentadoria", "auxilio"]


def test_read_resource_xlsx_with_banner_inside_zip(tmp_path, make_xlsx_bytes):
    # Exercises the io.BytesIO branch of _read_zip, where the peek at the header
    # leaves the stream at EOF unless it is rewound.
    buffer_path = tmp_path / "res.zip"
    with zipfile.ZipFile(buffer_path, "w") as archive:
        archive.writestr("dados.xlsx", make_xlsx_bytes(ROWS, banner=BANNER))

    df = read_resource(buffer_path, _entry(), columns=None, engine=XlsxEngine.OPENPYXL)

    assert list(df.columns) == ["periodo_referencia", "beneficio", "valor"]
    assert len(df) == 2


def test_read_resource_legacy_xls_skips_banner_row(tmp_path, make_xls_bytes):
    path = _write(tmp_path, "res.xls", make_xls_bytes(ROWS, banner=BANNER))
    df = read_resource(path, _entry(format_="XLS"), columns=None, engine=XlsxEngine.OPENPYXL)

    assert list(df.columns) == ["periodo_referencia", "beneficio", "valor"]
    assert len(df) == 2
