from __future__ import annotations

import pandas as pd
import pytest
import responses
from responses import matchers

from brinss.datasets import _ckan, _loader
from brinss.datasets.exceptions import ColumnNotFoundError, PeriodUnavailableError

SLUG = "beneficios-concedidos-plano-de-dados-abertos-jun-2023-a-jun-2025"


def _package_show_fixture(resources: list[dict]) -> dict:
    return {"success": True, "result": {"name": SLUG, "resources": resources}}


def _mock_package_show(resources: list[dict]) -> None:
    responses.add(
        responses.GET,
        f"{_ckan.BASE_URL}/api/3/action/package_show",
        json=_package_show_fixture(resources),
        status=200,
        match=[matchers.query_param_matcher({"id": SLUG})],
    )


def _resource(period_name: str, resource_id: str, url: str) -> dict:
    return {"id": resource_id, "name": period_name, "format": "XLSX", "url": url}


@responses.activate
def test_load_dataset_default_returns_only_latest_period(cache_dir, make_xlsx_bytes):
    resources = [
        _resource("Benefícios concedidos maio 2024", "res-05", "https://fixtures.test/concedidos/maio-2024.xlsx"),
        _resource("Benefícios concedidos junho 2024", "res-06", "https://fixtures.test/concedidos/junho-2024.xlsx"),
    ]
    _mock_package_show(resources)
    responses.add(
        responses.GET,
        resources[1]["url"],
        body=make_xlsx_bytes([{"beneficio": "aposentadoria", "valor": 1500}]),
        status=200,
    )
    # deliberately no mock for the maio-2024 URL: if the loader fetched it, the test would fail

    df = _loader.load_dataset("beneficios_concedidos", cache_dir=cache_dir)

    assert list(df["periodo_referencia"]) == [pd.Period("2024-06", freq="M")]
    assert df.loc[0, "beneficio"] == "aposentadoria"


@responses.activate
def test_load_dataset_range_concatenates_and_as_dict(cache_dir, make_xlsx_bytes):
    resources = [
        _resource("Benefícios concedidos maio 2024", "res-05", "https://fixtures.test/concedidos/maio-2024.xlsx"),
        _resource("Benefícios concedidos junho 2024", "res-06", "https://fixtures.test/concedidos/junho-2024.xlsx"),
    ]
    _mock_package_show(resources)
    responses.add(
        responses.GET, resources[0]["url"], body=make_xlsx_bytes([{"beneficio": "auxilio", "valor": 900}]), status=200
    )
    responses.add(
        responses.GET,
        resources[1]["url"],
        body=make_xlsx_bytes([{"beneficio": "aposentadoria", "valor": 1500}]),
        status=200,
    )

    df = _loader.load_dataset("beneficios_concedidos", periodo=("2024-05", "2024-06"), cache_dir=cache_dir)
    assert len(df) == 2
    assert set(df["periodo_referencia"]) == {pd.Period("2024-05", freq="M"), pd.Period("2024-06", freq="M")}

    result = _loader.load_dataset(
        "beneficios_concedidos", periodo=("2024-05", "2024-06"), as_dict=True, cache_dir=cache_dir
    )
    assert set(result) == {"2024-05", "2024-06"}
    assert isinstance(result["2024-06"], pd.DataFrame)


@responses.activate
def test_load_dataset_handles_csv_labeled_resource_that_is_actually_a_zip(cache_dir, make_csv_zip_bytes):
    # Mirrors the real portal: beneficios_emitidos/mantidos resources are labeled
    # "CSV" in CKAN metadata but the actual download is a ZIP wrapping one CSV.
    resources = [
        {
            "id": "res-06",
            "name": "Benefícios concedidos junho 2024",
            "format": "CSV",
            "url": "https://fixtures.test/concedidos/junho-2024.csv.zip",
        }
    ]
    _mock_package_show(resources)
    responses.add(
        responses.GET,
        resources[0]["url"],
        body=make_csv_zip_bytes([{"beneficio": "aposentadoria", "valor": 1500}], member_name="dados.csv"),
        status=200,
    )

    df = _loader.load_dataset("beneficios_concedidos", cache_dir=cache_dir)

    assert df.loc[0, "beneficio"] == "aposentadoria"
    assert list(df["periodo_referencia"]) == [pd.Period("2024-06", freq="M")]


@responses.activate
def test_load_dataset_missing_column_raises(cache_dir, make_xlsx_bytes):
    resources = [
        _resource("Benefícios concedidos junho 2024", "res-06", "https://fixtures.test/concedidos/junho-2024.xlsx"),
    ]
    _mock_package_show(resources)
    responses.add(
        responses.GET,
        resources[0]["url"],
        body=make_xlsx_bytes([{"beneficio": "aposentadoria", "valor": 1500}]),
        status=200,
    )

    with pytest.raises(ColumnNotFoundError):
        _loader.load_dataset("beneficios_concedidos", columns=["coluna_inexistente"], cache_dir=cache_dir)


@responses.activate
def test_load_dataset_unavailable_period_raises(cache_dir):
    resources = [
        _resource("Benefícios concedidos junho 2024", "res-06", "https://fixtures.test/concedidos/junho-2024.xlsx"),
    ]
    _mock_package_show(resources)

    with pytest.raises(PeriodUnavailableError):
        _loader.load_dataset("beneficios_concedidos", periodo="2020-01", cache_dir=cache_dir)


def test_load_dataset_unknown_family_raises_key_error(cache_dir):
    with pytest.raises(KeyError):
        _loader.load_dataset("familia-inexistente", cache_dir=cache_dir)


@responses.activate
def test_load_dataset_defaults_to_string_columns(cache_dir, make_xlsx_bytes):
    resources = [
        _resource("Benefícios concedidos junho 2024", "res-06", "https://fixtures.test/concedidos/junho-2024.xlsx"),
    ]
    _mock_package_show(resources)
    responses.add(
        responses.GET,
        resources[0]["url"],
        body=make_xlsx_bytes([{"beneficio": "aposentadoria", "valor": 1500}]),
        status=200,
    )

    df = _loader.load_dataset("beneficios_concedidos", cache_dir=cache_dir)

    assert df.loc[0, "valor"] == "1500"


@responses.activate
def test_load_dataset_dtype_infer_reaches_the_reader(cache_dir, make_xlsx_bytes):
    resources = [
        _resource("Benefícios concedidos junho 2024", "res-06", "https://fixtures.test/concedidos/junho-2024.xlsx"),
    ]
    _mock_package_show(resources)
    responses.add(
        responses.GET,
        resources[0]["url"],
        body=make_xlsx_bytes([{"beneficio": "aposentadoria", "valor": 1500}]),
        status=200,
    )

    df = _loader.load_dataset("beneficios_concedidos", dtype="infer", cache_dir=cache_dir)

    assert df.loc[0, "valor"] == 1500


def test_load_dataset_invalid_dtype_raises_before_downloading(cache_dir):
    # No responses are registered: reaching the network at all would error
    # differently, which is the point -- the check happens up front.
    with pytest.raises(ValueError, match="dtype invalido"):
        _loader.load_dataset("beneficios_concedidos", dtype="texto", cache_dir=cache_dir)
