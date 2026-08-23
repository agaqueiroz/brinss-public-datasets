from __future__ import annotations

import logging

import pandas as pd
import pytest
import responses
from responses import matchers

from brinss.datasets import _cache, _ckan, _loader, _log, _reading
from brinss.datasets._catalog import ResourceEntry
from brinss.datasets.enums import XlsxEngine
from brinss.datasets.exceptions import ColumnNotFoundError

SLUG = "beneficios-concedidos-plano-de-dados-abertos-jun-2023-a-jun-2025"


def _entry(url: str, *, resource_id: str = "res-1") -> ResourceEntry:
    return ResourceEntry(
        period=pd.Period("2024-06", freq="M"),
        url=url,
        resource_id=resource_id,
        resource_name="Beneficios concedidos junho 2024",
        package_slug="slug",
        format="XLSX",
    )


def _messages(logs) -> list[str]:
    return [record.getMessage() for record in logs.records if record.name == _log.LOGGER_NAME]


@responses.activate
def test_download_logs_completion_then_cache_hit(cache_dir, brinss_logs):
    entry = _entry("https://fixtures.test/concedidos/junho-2024.xlsx")
    responses.add(responses.GET, entry.url, body=b"fake-xlsx-bytes", status=200)

    _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)

    messages = _messages(brinss_logs)
    assert len(messages) == 1
    assert messages[0].startswith("Download complete:")
    assert "15 B" in messages[0]

    brinss_logs.clear()
    responses.reset()  # a real network call here would fail

    _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)

    messages = _messages(brinss_logs)
    assert len(messages) == 1
    assert messages[0].startswith("Using cached file")
    assert "Download complete" not in messages[0]


@responses.activate
def test_force_download_logs_completion_again(cache_dir, brinss_logs):
    entry = _entry("https://fixtures.test/concedidos/junho-2024.xlsx")
    responses.add(responses.GET, entry.url, body=b"first-version", status=200)
    _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)

    brinss_logs.clear()
    responses.reset()
    responses.add(responses.GET, entry.url, body=b"second-version", status=200)
    _cache.fetch_resource(
        entry, family_key="beneficios_concedidos", cache_dir=cache_dir, force_download=True
    )

    assert [message.split(":")[0] for message in _messages(brinss_logs)] == ["Download complete"]


def test_read_resource_logs_start_and_finish(tmp_path, make_csv_bytes, brinss_logs):
    path = tmp_path / "concedidos.csv"
    path.write_bytes(make_csv_bytes([{"beneficio": "aposentadoria", "valor": 1500}] * 3))

    _reading.read_resource(path, _entry("https://fixtures.test/x.csv"), columns=None, engine=XlsxEngine.OPENPYXL)

    start, finish = _messages(brinss_logs)
    assert start.startswith("Reading 'concedidos.csv'")
    assert start.endswith("into a DataFrame...")
    # 3 rows, and 3 columns counting the periodo_referencia the reader inserts
    assert finish.startswith("DataFrame loaded: 3 rows x 3 columns from 'concedidos.csv' in ")


def test_read_resource_logs_nothing_on_failure(tmp_path, make_csv_bytes, brinss_logs):
    path = tmp_path / "concedidos.csv"
    path.write_bytes(make_csv_bytes([{"beneficio": "aposentadoria", "valor": 1500}]))

    with pytest.raises(ColumnNotFoundError):
        _reading.read_resource(
            path, _entry("https://fixtures.test/x.csv"), columns=["inexistente"], engine=XlsxEngine.OPENPYXL
        )

    assert all("DataFrame loaded" not in message for message in _messages(brinss_logs))


@responses.activate
def test_load_dataset_logs_concatenation_only_for_multiple_periods(cache_dir, make_xlsx_bytes, brinss_logs):
    resources = [
        {
            "id": "res-05",
            "name": "Benefícios concedidos maio 2024",
            "format": "XLSX",
            "url": "https://fixtures.test/concedidos/maio-2024.xlsx",
        },
        {
            "id": "res-06",
            "name": "Benefícios concedidos junho 2024",
            "format": "XLSX",
            "url": "https://fixtures.test/concedidos/junho-2024.xlsx",
        },
    ]
    responses.add(
        responses.GET,
        f"{_ckan.BASE_URL}/api/3/action/package_show",
        json={"success": True, "result": {"name": SLUG, "resources": resources}},
        status=200,
        match=[matchers.query_param_matcher({"id": SLUG})],
    )
    for resource in resources:
        responses.add(
            responses.GET, resource["url"], body=make_xlsx_bytes([{"beneficio": "auxilio", "valor": 900}]), status=200
        )

    _loader.load_dataset("beneficios_concedidos", periodo=("2024-05", "2024-06"), cache_dir=cache_dir)
    assert any(
        message.startswith("Concatenated 2 periods: 2 rows x 3 columns in ") for message in _messages(brinss_logs)
    )

    brinss_logs.clear()
    _loader.load_dataset("beneficios_concedidos", periodo="2024-06", cache_dir=cache_dir)
    assert all("Concatenated" not in message for message in _messages(brinss_logs))


@responses.activate
def test_messages_can_be_silenced(cache_dir, caplog):
    logger = _log.get_logger()
    logger.addHandler(caplog.handler)
    original_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        entry = _entry("https://fixtures.test/concedidos/junho-2024.xlsx")
        responses.add(responses.GET, entry.url, body=b"fake-xlsx-bytes", status=200)
        _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)
    finally:
        logger.setLevel(original_level)
        logger.removeHandler(caplog.handler)

    assert _messages(caplog) == []


def test_format_helpers():
    assert _log.format_bytes(512) == "512 B"
    assert _log.format_bytes(1536) == "1.5 KB"
    assert _log.format_bytes(12 * 1024**2) == "12.0 MB"
    assert _log.format_bytes(3 * 1024**4) == "3.0 TB"

    assert _log.format_seconds(0.83) == "0.8 s"
    assert _log.format_seconds(59.9) == "59.9 s"
    assert _log.format_seconds(72.4) == "1 min 12 s"
