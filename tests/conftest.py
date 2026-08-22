from __future__ import annotations

import io
import json
from pathlib import Path

import openpyxl
import pytest

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
