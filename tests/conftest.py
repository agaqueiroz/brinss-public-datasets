from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import openpyxl
import pytest
import xlwt

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def load_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def make_xlsx_bytes():
    def _make(rows: list[dict]) -> bytes:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        if rows:
            headers = list(rows[0].keys())
            sheet.append(headers)
            for row in rows:
                sheet.append([row[header] for header in headers])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    return _make


@pytest.fixture
def make_csv_bytes():
    def _make(rows: list[dict], *, delimiter: str = ";", encoding: str = "latin-1") -> bytes:
        headers = list(rows[0].keys())
        lines = [delimiter.join(headers)]
        lines.extend(delimiter.join(str(row[header]) for header in headers) for row in rows)
        return ("\r\n".join(lines) + "\r\n").encode(encoding)

    return _make


@pytest.fixture
def make_csv_zip_bytes(make_csv_bytes):
    def _make(
        rows: list[dict],
        *,
        delimiter: str = ";",
        encoding: str = "latin-1",
        member_name: str = "dados.csv",
        extra_members: dict[str, bytes] | None = None,
    ) -> bytes:
        csv_bytes = make_csv_bytes(rows, delimiter=delimiter, encoding=encoding)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(member_name, csv_bytes)
            for name, content in (extra_members or {}).items():
                archive.writestr(name, content)
        return buffer.getvalue()

    return _make


@pytest.fixture
def make_xls_bytes():
    def _make(rows: list[dict]) -> bytes:
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Sheet1")
        if rows:
            headers = list(rows[0].keys())
            for col, header in enumerate(headers):
                sheet.write(0, col, header)
            for row_idx, row in enumerate(rows, start=1):
                for col, header in enumerate(headers):
                    sheet.write(row_idx, col, row[header])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    return _make
