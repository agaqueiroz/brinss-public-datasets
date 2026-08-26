from __future__ import annotations

import pandas as pd
import pytest
import responses

from brinss.datasets import _catalog, _hf, _loader
from brinss.datasets._families import FAMILIES
from brinss.datasets.enums import DataSource
from brinss.datasets.exceptions import (
    ColumnNotFoundError,
    HuggingFaceUnavailableError,
    PeriodUnavailableError,
)

FAMILY = "beneficios_concedidos"


def _manifest(*periods: str, family: str = FAMILY) -> dict:
    return {
        "schema_version": _hf.MANIFEST_SCHEMA_VERSION,
        "entries": {
            _hf.manifest_key(family, period): {
                "source_sha256": f"sha256:{period}",
                "source_url": f"https://fixtures.test/{family}/{period}.xlsx",
                "resource_id": f"res-{period}",
                "rows": 1,
                "columns": 2,
                "parquet_bytes": 1,
                "conversion": "v1|dtype=str|compression=zstd|period=str",
            }
            for period in periods
        },
    }


def _mock_manifest(payload: dict) -> None:
    responses.add(responses.GET, _hf.resolve_url(_hf.MANIFEST_PATH), json=payload, status=200)


def _mock_parquet(period: str, body: bytes, *, family: str = FAMILY) -> None:
    responses.add(
        responses.GET, _hf.resolve_url(_hf.path_in_repo(family, period)), body=body, status=200
    )


@responses.activate
def test_load_dataset_defaults_to_the_mirror_and_its_latest_period(cache_dir, make_parquet_bytes):
    _mock_manifest(_manifest("2024-05", "2024-06"))
    _mock_parquet("2024-06", make_parquet_bytes([{"beneficio": "aposentadoria"}], period="2024-06"))

    df = _loader.load_dataset(FAMILY, cache_dir=cache_dir)

    assert list(df["beneficio"]) == ["aposentadoria"]
    # Only the requested month was fetched: the mirror is one file per month.
    assert [call.request.url for call in responses.calls].count(
        _hf.resolve_url(_hf.path_in_repo(FAMILY, "2024-05"))
    ) == 0


@responses.activate
def test_periodo_referencia_comes_back_as_a_period_not_the_stored_string(cache_dir, make_parquet_bytes):
    """The mirror stores it as "2024-06"; both sources must hand back a Period."""
    _mock_manifest(_manifest("2024-06"))
    _mock_parquet("2024-06", make_parquet_bytes([{"beneficio": "auxilio"}], period="2024-06"))

    df = _loader.load_dataset(FAMILY, cache_dir=cache_dir)

    assert next(iter(df.columns)) == "periodo_referencia"
    assert list(df["periodo_referencia"]) == [pd.Period("2024-06", freq="M")]
    # And exactly once -- the stored text column must not survive alongside it.
    assert list(df.columns).count("periodo_referencia") == 1


@responses.activate
def test_load_dataset_range_concatenates_and_as_dict(cache_dir, make_parquet_bytes):
    _mock_manifest(_manifest("2024-05", "2024-06"))
    _mock_parquet("2024-05", make_parquet_bytes([{"beneficio": "auxilio"}], period="2024-05"))
    _mock_parquet("2024-06", make_parquet_bytes([{"beneficio": "aposentadoria"}], period="2024-06"))

    df = _loader.load_dataset(FAMILY, periodo=("2024-05", "2024-06"), cache_dir=cache_dir)
    assert len(df) == 2
    assert set(df["periodo_referencia"]) == {pd.Period("2024-05", freq="M"), pd.Period("2024-06", freq="M")}

    result = _loader.load_dataset(FAMILY, periodo="all", as_dict=True, cache_dir=cache_dir)
    assert set(result) == {"2024-05", "2024-06"}


@responses.activate
def test_load_dataset_columns_selects_a_subset(cache_dir, make_parquet_bytes):
    _mock_manifest(_manifest("2024-06"))
    _mock_parquet(
        "2024-06", make_parquet_bytes([{"beneficio": "auxilio", "valor": 900}], period="2024-06")
    )

    df = _loader.load_dataset(FAMILY, columns=["valor"], cache_dir=cache_dir)

    assert list(df.columns) == ["periodo_referencia", "valor"]


@responses.activate
def test_load_dataset_missing_column_raises(cache_dir, make_parquet_bytes):
    _mock_manifest(_manifest("2024-06"))
    _mock_parquet("2024-06", make_parquet_bytes([{"beneficio": "auxilio"}], period="2024-06"))

    with pytest.raises(ColumnNotFoundError):
        _loader.load_dataset(FAMILY, columns=["coluna_inexistente"], cache_dir=cache_dir)


@responses.activate
def test_load_dataset_defaults_to_string_columns(cache_dir, make_parquet_bytes):
    _mock_manifest(_manifest("2024-06"))
    _mock_parquet(
        "2024-06", make_parquet_bytes([{"cid": "01234", "valor": 900}], period="2024-06")
    )

    df = _loader.load_dataset(FAMILY, cache_dir=cache_dir)

    assert list(df["cid"]) == ["01234"]  # the leading zero survives
    assert list(df["valor"]) == ["900"]


@responses.activate
def test_load_dataset_dtype_infer_converts_after_reading(cache_dir, make_parquet_bytes):
    """The mirror's columns are all text, so inference happens on the frame."""
    _mock_manifest(_manifest("2024-06"))
    _mock_parquet(
        "2024-06",
        make_parquet_bytes([{"cid": "01234", "valor": 900, "beneficio": "auxilio"}], period="2024-06"),
    )

    df = _loader.load_dataset(FAMILY, dtype="infer", cache_dir=cache_dir)

    assert df["valor"].iloc[0] == 900
    assert df["cid"].iloc[0] == 1234  # inference drops the leading zero, as on the portal
    assert df["beneficio"].iloc[0] == "auxilio"  # genuinely text, left alone
    assert list(df["periodo_referencia"]) == [pd.Period("2024-06", freq="M")]


@responses.activate
def test_load_dataset_unavailable_period_points_at_the_other_source(cache_dir):
    _mock_manifest(_manifest("2024-06"))

    with pytest.raises(PeriodUnavailableError, match="fonte 'inss'"):
        _loader.load_dataset(FAMILY, periodo="2026-08", cache_dir=cache_dir)


@responses.activate
def test_load_dataset_invalid_source_raises_before_downloading(cache_dir):
    # Nothing is registered with responses: reaching the network would fail
    # differently, which is the point.
    with pytest.raises(ValueError, match="source invalido"):
        _loader.load_dataset(FAMILY, source="hugging-face", cache_dir=cache_dir)


@responses.activate
def test_list_periods_reads_the_manifest(cache_dir):
    _mock_manifest(_manifest("2024-06", "2024-05"))

    assert _loader.list_periods(FAMILY, cache_dir=cache_dir) == [
        pd.Period("2024-05", freq="M"),
        pd.Period("2024-06", freq="M"),
    ]


@responses.activate
def test_build_catalog_ignores_other_families_and_unparseable_periods(cache_dir):
    payload = _manifest("2024-06")
    payload["entries"].update(_manifest("2024-06", family="perfil_unidades")["entries"])
    payload["entries"][_hf.manifest_key(FAMILY, "nao-e-um-mes")] = {"resource_id": "x"}
    _mock_manifest(payload)

    with pytest.warns(UserWarning, match="nao tem periodo reconhecivel"):
        catalog = _catalog.build_catalog(
            FAMILIES[FAMILY], cache_dir=cache_dir, source=DataSource.HF
        )

    assert [entry.period for entry in catalog.entries] == [pd.Period("2024-06", freq="M")]
    assert catalog.entries[0].url.endswith(f"data/{FAMILY}/2024-06.parquet")


@responses.activate
def test_build_catalog_falls_back_to_stale_cache_with_a_warning(cache_dir):
    _mock_manifest(_manifest("2024-06"))
    _catalog.build_catalog(FAMILIES[FAMILY], cache_dir=cache_dir, source=DataSource.HF)

    responses.reset()
    responses.add(responses.GET, _hf.resolve_url(_hf.MANIFEST_PATH), status=503)

    with pytest.warns(UserWarning, match="Hugging Face indisponivel"):
        catalog = _catalog.build_catalog(
            FAMILIES[FAMILY], cache_dir=cache_dir, source=DataSource.HF, force_refresh=True
        )

    assert [entry.period for entry in catalog.entries] == [pd.Period("2024-06", freq="M")]


@responses.activate
def test_build_catalog_raises_when_the_mirror_is_unreachable_and_there_is_no_cache(cache_dir):
    responses.add(responses.GET, _hf.resolve_url(_hf.MANIFEST_PATH), status=503)

    with pytest.raises(HuggingFaceUnavailableError):
        _catalog.build_catalog(FAMILIES[FAMILY], cache_dir=cache_dir, source=DataSource.HF)


@responses.activate
def test_manifest_with_an_unknown_schema_version_is_refused(cache_dir):
    payload = _manifest("2024-06")
    payload["schema_version"] = _hf.MANIFEST_SCHEMA_VERSION + 1
    _mock_manifest(payload)

    with pytest.raises(HuggingFaceUnavailableError, match="schema_version"):
        _catalog.build_catalog(FAMILIES[FAMILY], cache_dir=cache_dir, source=DataSource.HF)


def test_repo_paths_round_trip():
    assert _hf.path_in_repo(FAMILY, "2024-06") == f"data/{FAMILY}/2024-06.parquet"
    assert _hf.parse_repo_path(f"data/{FAMILY}/2024-06.parquet") == (FAMILY, "2024-06")
    assert _hf.parse_repo_path("manifest.json") is None
