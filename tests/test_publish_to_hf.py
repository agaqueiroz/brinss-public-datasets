from __future__ import annotations

import hashlib
import io

import pandas as pd
import publish_to_hf as publish
import pyarrow.parquet as pq
import pytest


def _manifest(**overrides) -> dict:
    entry = {
        "source_sha256": "sha256:abc",
        "conversion": publish.CONVERSION_RECIPE,
    }
    entry.update(overrides)
    return {"schema_version": 1, "entries": {"beneficios_concedidos/2024-06": entry}}


def test_sha256_file_matches_hashlib(tmp_path):
    # The chunked reader exists so multi-GB resources do not land in memory;
    # it still has to agree with hashing the whole file at once.
    path = tmp_path / "res.bin"
    payload = b"x" * (publish._HASH_CHUNK_BYTES * 2 + 17)
    path.write_bytes(payload)

    assert publish.sha256_file(path) == f"sha256:{hashlib.sha256(payload).hexdigest()}"


def test_path_in_repo_is_one_file_per_period():
    assert publish.path_in_repo("beneficios_concedidos", "2024-06") == (
        "data/beneficios_concedidos/2024-06.parquet"
    )


def test_needs_upload_for_a_period_absent_from_the_manifest():
    should, reason = publish.needs_upload(publish.empty_manifest(), "beneficios_concedidos", "2024-06", "sha256:abc")

    assert should is True
    assert reason == "novo"


def test_needs_upload_skips_when_source_hash_is_unchanged():
    should, reason = publish.needs_upload(_manifest(), "beneficios_concedidos", "2024-06", "sha256:abc")

    assert should is False
    assert reason == "inalterado"


def test_needs_upload_when_the_portal_replaced_the_file():
    # The whole point of the manifest: the government swaps a file's contents
    # at the same URL without renaming it.
    should, reason = publish.needs_upload(_manifest(), "beneficios_concedidos", "2024-06", "sha256:outro")

    assert should is True
    assert reason == "origem mudou"


def test_needs_upload_when_the_conversion_recipe_changed():
    # A recipe bump has to reach the Hub even though the source is untouched --
    # otherwise already published files stay on the old conversion forever.
    manifest = _manifest(conversion="v0|dtype=infer|compression=snappy|period=str")
    should, reason = publish.needs_upload(manifest, "beneficios_concedidos", "2024-06", "sha256:abc")

    assert should is True
    assert reason == "conversao mudou"


def test_needs_upload_isolates_families_that_share_a_period():
    # "mantidos" publishes ativos/cessados/suspensos for the same month, so the
    # key has to carry the family and not just the period.
    should, _ = publish.needs_upload(_manifest(), "beneficios_mantidos_ativos", "2024-06", "sha256:abc")

    assert should is True


def test_to_publishable_writes_the_period_as_text():
    # pandas stores a Period column in Parquet as an extension type over a raw
    # month ordinal (2024-06 -> 653), which every non-pandas reader -- the Hub
    # viewer included -- shows as a meaningless integer.
    frame = pd.DataFrame(
        {"periodo_referencia": [pd.Period("2024-06", freq="M")] * 2, "codigo": ["01234", "00987"]}
    )

    buffer = io.BytesIO()
    publish.to_publishable(frame).to_parquet(buffer, engine="pyarrow", index=False)
    table = pq.read_table(io.BytesIO(buffer.getvalue()))

    assert table.column("periodo_referencia").to_pylist() == ["2024-06", "2024-06"]
    assert table.column("codigo").to_pylist() == ["01234", "00987"]  # leading zeros survive


def test_to_publishable_does_not_mutate_the_caller_frame():
    frame = pd.DataFrame({"periodo_referencia": [pd.Period("2024-06", freq="M")]})
    publish.to_publishable(frame)

    assert frame["periodo_referencia"].dtype == "period[M]"


def test_dataset_card_declares_one_viewer_config_per_family():
    card = publish.build_dataset_card(["beneficios_concedidos", "perfil_unidades"])

    assert "- config_name: beneficios_concedidos" in card
    assert "    path: data/perfil_unidades/*.parquet" in card
    assert card.startswith("---\n")  # the YAML block has to be the very first thing


@pytest.mark.parametrize(
    ("size", "expected"),
    [(512, "512 B"), (2048, "2.0 KB"), (5 * 1024**2, "5.0 MB"), (3 * 1024**3, "3.0 GB")],
)
def test_format_bytes(size, expected):
    assert publish.format_bytes(size) == expected
