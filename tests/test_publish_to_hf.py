from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pandas as pd
import publish_to_hf as publish
import pyarrow.parquet as pq
import pytest

from brinss.datasets import _cache
from brinss.datasets._catalog import ResourceEntry


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


def test_to_publishable_copy_is_shallow(monkeypatch):
    # A deep copy here would duplicate every column of a frame that already runs
    # to gigabytes on the heavy families, just to rewrite one column.
    frame = pd.DataFrame({"periodo_referencia": [pd.Period("2024-06", freq="M")], "x": ["a"]})
    seen = {}
    original = pd.DataFrame.copy

    def spy(self, deep=True):
        seen["deep"] = deep
        return original(self, deep=deep)

    monkeypatch.setattr(pd.DataFrame, "copy", spy)
    publish.to_publishable(frame)

    assert seen["deep"] is False


@pytest.mark.parametrize(
    ("given", "expected"),
    [(["2024-06"], ["2024-06"]), (["2024-6"], ["2024-06"]), (None, None), ([], None)],
)
def test_normalize_periodos_accepts_and_pads(given, expected):
    # "2024-6" used to match no entry at all, so the run ended looking like a
    # legitimate "nothing to do".
    assert publish.normalize_periodos(given) == expected


@pytest.mark.parametrize("given", ["2024-13", "junho", ""])
def test_normalize_periodos_rejects_garbage(given):
    with pytest.raises(SystemExit, match="periodo invalido"):
        publish.normalize_periodos([given])


def test_published_families_only_lists_what_the_manifest_has():
    # A viewer config whose glob matches no file is a broken entry, so a partial
    # rollout must not advertise families it has not reached.
    manifest = {
        "entries": {
            "perfil_unidades/2026-07": {},
            "beneficios_concedidos/2024-06": {},
            "familia_que_sumiu/2024-06": {},
        }
    }

    assert publish.published_families(manifest) == ["beneficios_concedidos", "perfil_unidades"]


def test_published_families_of_an_empty_manifest():
    assert publish.published_families(publish.empty_manifest()) == []


def test_cached_source_finds_a_file_written_under_the_library_layout(tmp_path):
    # Pins the private coupling: _cached_source rebuilds the cache path itself so
    # a dry run does not have to download. A rename in _cache must fail here
    # rather than silently reporting every cached month as missing.
    entry = ResourceEntry(
        period=pd.Period("2024-06", freq="M"),
        url="https://fixtures.test/res.xlsx",
        resource_id="res-1",
        resource_name="Beneficios concedidos junho 2024",
        package_slug="slug",
        format="XLSX",
    )
    target = tmp_path / "files" / "beneficios_concedidos" / _cache._resource_filename(entry)
    target.parent.mkdir(parents=True)

    assert publish._cached_source(entry, "beneficios_concedidos", tmp_path) is None

    target.write_bytes(b"conteudo")

    assert publish._cached_source(entry, "beneficios_concedidos", tmp_path) == target


def test_commit_batch_flushes_on_the_file_count(tmp_path):
    # One upload per month meant one commit per month: ~300 on a first load.
    commits = []

    class FakeApi:
        def create_commit(self, *, repo_id, repo_type, operations, commit_message):
            commits.append([op.path_in_repo for op in operations])

    manifest = publish.empty_manifest()
    batch = publish.CommitBatch(api=FakeApi(), repo_id="r", manifest=manifest, max_files=2)

    for index in range(3):
        local = tmp_path / f"{index}.parquet"
        local.write_bytes(b"x")
        batch.add(local, f"data/f/{index}.parquet", (f"f/{index}", {"rows": index}), size=1)

    assert commits == [["data/f/0.parquet", "data/f/1.parquet"]]
    assert sorted(manifest["entries"]) == ["f/0", "f/1"]  # only what actually landed

    batch.flush()

    assert commits[-1] == ["data/f/2.parquet"]
    assert sorted(manifest["entries"]) == ["f/0", "f/1", "f/2"]


def test_commit_batch_flush_is_a_noop_when_empty():
    class ExplodingApi:
        def create_commit(self, **kwargs):
            raise AssertionError("nao deveria commitar nada")

    publish.CommitBatch(api=ExplodingApi(), repo_id="r", manifest=publish.empty_manifest()).flush()


def test_commit_batch_removes_staged_files_after_the_commit(tmp_path):
    # The staging directory holds whole months of Parquet; leaving them behind
    # until the run ends would defeat the byte cap.
    class FakeApi:
        def create_commit(self, **kwargs):
            pass

    batch = publish.CommitBatch(api=FakeApi(), repo_id="r", manifest=publish.empty_manifest(), max_files=1)
    local = tmp_path / "a.parquet"
    local.write_bytes(b"x")
    batch.add(local, "data/f/a.parquet", ("f/a", {}), size=1)

    assert not local.exists()


def test_no_hub_with_push_is_rejected(capsys):
    code = publish.main(["--no-hub", "--push"])

    assert code == 2
    assert "incompativeis" in capsys.readouterr().err


def test_push_without_any_token_is_rejected(monkeypatch, capsys):
    # The gate used to read os.environ directly, so a user authenticated by
    # `huggingface-cli login` -- token on disk, no env var -- was refused.
    # get_token() covers both, which is why it is what gets stubbed here.
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    code = publish.main(["--push", "--familia", "perfil_unidades"])

    assert code == 2
    err = capsys.readouterr().err
    assert "HF_TOKEN" in err
    assert "huggingface-cli login" in err  # the disk-stored token is a valid source


def _sample_args(tmp_path, *, force=False):
    return SimpleNamespace(sample_dir=str(tmp_path / "tmp"), force=force)


def _entry() -> ResourceEntry:
    return ResourceEntry(
        period=pd.Period("2024-06", freq="M"),
        url="https://fixtures.test/res.csv",
        resource_id="res-1",
        resource_name="Perfil das Unidades Junho 2024",
        package_slug="slug",
        format="CSV",
    )


def test_sample_mirrors_the_repo_layout_under_the_sample_dir(tmp_path, make_csv_bytes):
    # The local tree matches path_in_repo so what you inspect is exactly what
    # the Hub would receive.
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234", "valor": 1500}]))
    plan = publish.Plan()
    target = publish.path_in_repo("perfil_unidades", "2024-06")

    publish._sample_one(_sample_args(tmp_path), _entry(), "perfil_unidades", "2024-06", source, target, plan)

    written = tmp_path / "tmp" / "data" / "perfil_unidades" / "2024-06.parquet"
    assert written.exists()
    assert plan.uploads == [("perfil_unidades", "2024-06", "amostra")]
    assert plan.written_bytes == written.stat().st_size

    table = pq.read_table(written)
    assert table.column("codigo").to_pylist() == ["01234"]
    assert table.column("periodo_referencia").to_pylist() == ["2024-06"]


def test_sample_skips_a_month_that_is_not_cached(tmp_path):
    # --sample must never download: that is the whole reason it exists, since a
    # full run pulls tens of GB from the portal.
    plan = publish.Plan()

    publish._sample_one(_sample_args(tmp_path), _entry(), "perfil_unidades", "2024-06", None, "t", plan)

    assert plan.skipped == [("perfil_unidades", "2024-06")]
    assert plan.uploads == []
    assert not (tmp_path / "tmp").exists()


def test_sample_does_not_redo_an_existing_file_unless_forced(tmp_path, make_csv_bytes):
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234"}]))
    target = publish.path_in_repo("perfil_unidades", "2024-06")
    written = tmp_path / "tmp" / "data" / "perfil_unidades" / "2024-06.parquet"
    written.parent.mkdir(parents=True)
    written.write_bytes(b"placeholder")

    plan = publish.Plan()
    publish._sample_one(_sample_args(tmp_path), _entry(), "perfil_unidades", "2024-06", source, target, plan)

    assert plan.skipped == [("perfil_unidades", "2024-06")]
    assert written.read_bytes() == b"placeholder"

    forced = publish.Plan()
    publish._sample_one(
        _sample_args(tmp_path, force=True), _entry(), "perfil_unidades", "2024-06", source, target, forced
    )

    assert forced.uploads == [("perfil_unidades", "2024-06", "amostra")]
    assert written.read_bytes() != b"placeholder"


def test_sample_with_push_is_rejected(capsys):
    code = publish.main(["--sample", "--push"])

    assert code == 2
    assert "incompativeis" in capsys.readouterr().err
