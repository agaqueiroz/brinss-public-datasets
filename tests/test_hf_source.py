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


def _mock_two_months() -> None:
    _mock_manifest(_manifest("2024-05", "2024-06"))


@responses.activate
def test_chunksize_streams_every_period_in_order_and_matches_the_whole_load(cache_dir, make_parquet_bytes):
    _mock_two_months()
    rows_05 = [{"beneficio": "auxilio"}, {"beneficio": "pensao"}]
    rows_06 = [{"beneficio": "aposentadoria"}, {"beneficio": "salario"}, {"beneficio": "bpc"}]
    _mock_parquet("2024-05", make_parquet_bytes(rows_05, period="2024-05"))
    _mock_parquet("2024-06", make_parquet_bytes(rows_06, period="2024-06"))

    chunks = _loader.load_dataset(FAMILY, periodo="all", chunksize=2, cache_dir=cache_dir)

    assert isinstance(chunks, _loader.DatasetChunks)
    frames = list(chunks)
    assert [len(frame) for frame in frames] == [2, 2, 1]
    assert [frame["periodo_referencia"].iloc[0] for frame in frames] == [
        pd.Period("2024-05", freq="M"),
        pd.Period("2024-06", freq="M"),
        pd.Period("2024-06", freq="M"),
    ]
    whole = _loader.load_dataset(FAMILY, periodo="all", cache_dir=cache_dir)
    pd.testing.assert_frame_equal(pd.concat(frames, ignore_index=True), whole)


@responses.activate
def test_chunksize_downloads_each_period_only_when_it_is_reached(cache_dir, make_parquet_bytes):
    _mock_two_months()
    _mock_parquet("2024-05", make_parquet_bytes([{"beneficio": "auxilio"}], period="2024-05"))
    _mock_parquet("2024-06", make_parquet_bytes([{"beneficio": "aposentadoria"}], period="2024-06"))
    june_url = _hf.resolve_url(_hf.path_in_repo(FAMILY, "2024-06"))

    chunks = _loader.load_dataset(FAMILY, periodo="all", chunksize=10, cache_dir=cache_dir)
    first = next(chunks)

    assert list(first["beneficio"]) == ["auxilio"]
    assert june_url not in [call.request.url for call in responses.calls]
    chunks.close()


@responses.activate
def test_chunksize_with_block_releases_the_cached_file_when_stopping_early(cache_dir, make_parquet_bytes):
    _mock_manifest(_manifest("2024-06"))
    rows = [{"beneficio": "auxilio"}, {"beneficio": "pensao"}]
    _mock_parquet("2024-06", make_parquet_bytes(rows, period="2024-06"))

    with _loader.load_dataset(FAMILY, chunksize=1, cache_dir=cache_dir) as chunks:
        for _ in chunks:
            break

    # On Windows a handle still open on the Parquet makes this fail outright.
    (cached,) = (cache_dir / "files" / FAMILY).glob("*.parquet")
    cached.unlink()


@responses.activate
def test_chunksize_reports_an_unavailable_period_at_the_call(cache_dir):
    _mock_manifest(_manifest("2024-06"))

    with pytest.raises(PeriodUnavailableError):
        _loader.load_dataset(FAMILY, periodo="2026-08", chunksize=100, cache_dir=cache_dir)


@pytest.mark.parametrize("chunksize", [0, -1, True, 1.5, "100"])
def test_invalid_chunksize_raises_before_reaching_the_network(cache_dir, chunksize):
    # Nothing is mocked: reaching the network would fail differently.
    with pytest.raises(ValueError, match="chunksize invalido"):
        _loader.load_dataset(FAMILY, chunksize=chunksize, cache_dir=cache_dir)


def test_chunksize_and_as_dict_do_not_combine(cache_dir):
    with pytest.raises(ValueError, match="as_dict"):
        _loader.load_dataset(FAMILY, chunksize=100, as_dict=True, cache_dir=cache_dir)


def test_repo_paths_round_trip():
    assert _hf.path_in_repo(FAMILY, "2024-06") == f"data/{FAMILY}/2024-06.parquet"
    assert _hf.parse_repo_path(f"data/{FAMILY}/2024-06.parquet") == (FAMILY, "2024-06")
    assert _hf.parse_repo_path("manifest.json") is None
