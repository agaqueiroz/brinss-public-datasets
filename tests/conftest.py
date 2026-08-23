from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path

import openpyxl
import pytest
import xlwt

from brinss.datasets import _log

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def brinss_logs(caplog):
    """Capture the library's progress messages.

    The ``brinss`` logger does not propagate to the root logger (so that an
    application's own logging config does not print every message twice),
    which also means caplog's root handler never sees it -- hence attaching
    that handler to the logger directly.
    """
    logger = _log.get_logger()
    caplog.set_level(logging.INFO, logger=_log.LOGGER_NAME)
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


@pytest.fixture
def load_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def make_xlsx_bytes():
    def _make(rows: list[dict], *, banner: str | None = None, headers: list[str] | None = None) -> bytes:
        """Build an .xlsx. With ``banner``, prepend a one-cell title row above
        the header, the way the real "beneficios" spreadsheets are published.
        ``headers`` overrides the column names, to exercise duplicates.
        """
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        if banner is not None:
            sheet.append([banner])
        if rows:
            keys = list(rows[0].keys())
            sheet.append(headers if headers is not None else keys)
            for row in rows:
                sheet.append([row[key] for key in keys])
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
    def _make(rows: list[dict], *, banner: str | None = None) -> bytes:
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Sheet1")
        offset = 0
        if banner is not None:
            sheet.write(0, 0, banner)
            offset = 1
        if rows:
            headers = list(rows[0].keys())
            for col, header in enumerate(headers):
                sheet.write(offset, col, header)
            for row_idx, row in enumerate(rows, start=offset + 1):
                for col, header in enumerate(headers):
                    sheet.write(row_idx, col, row[header])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    return _make
