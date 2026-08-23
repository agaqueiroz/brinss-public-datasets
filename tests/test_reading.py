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
