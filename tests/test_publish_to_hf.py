from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import publish_to_hf as publish
import pyarrow as pa
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


def _args(**overrides) -> argparse.Namespace:
    """Real parsed args, so a renamed flag breaks the tests instead of silently
    leaving an attribute the script reads with a stale value."""
    args = publish.build_parser().parse_args(["--no-log-file"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _entry(period: str = "2024-06", name: str = "Perfil das Unidades Junho 2024") -> ResourceEntry:
    return ResourceEntry(
        period=pd.Period(period, freq="M"),
        url="https://fixtures.test/res.csv",
        resource_id=f"res-{period}",
        resource_name=name,
        package_slug="slug",
        format="CSV",
    )


def _seed_source(cache_root: Path, family_key: str, entry: ResourceEntry, payload: bytes) -> Path:
    """Put a source file where the library's cache layout says it belongs."""
    path = cache_root / "files" / family_key / _cache._resource_filename(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


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


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("data/beneficios_concedidos/2024-06.parquet", ("beneficios_concedidos", "2024-06")),
        ("manifest.json", None),
        ("data/README.md", None),
        ("data/familia_que_sumiu/2024-06.parquet", None),
    ],
)
def test_parse_repo_path_round_trips_path_in_repo(path, expected):
    assert publish.parse_repo_path(path) == expected


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


# --------------------------------------------------------------------------
# commit batching: data and manifest land together or not at all
# --------------------------------------------------------------------------


class _RecordingApi:
    def __init__(self):
        self.commits = []

    def create_commit(self, *, repo_id, repo_type, operations, commit_message):
        self.commits.append(
            SimpleNamespace(
                paths=[op.path_in_repo for op in operations],
                message=commit_message,
                operations=operations,
            )
        )
        return SimpleNamespace(oid="0" * 40)


def _manifest_payload(commit) -> dict:
    for operation in commit.operations:
        if operation.path_in_repo == publish.MANIFEST_PATH:
            return json.loads(operation.path_or_fileobj.decode("utf-8"))
    raise AssertionError("o commit nao carregou o manifesto")


def test_commit_batch_flushes_on_the_file_count(tmp_path):
    # One upload per month meant one commit per month: ~300 on a first load.
    api = _RecordingApi()
    manifest = publish.empty_manifest()
    batch = publish.CommitBatch(api=api, repo_id="r", manifest=manifest, max_files=2)

    for index in range(3):
        local = tmp_path / f"{index}.parquet"
        local.write_bytes(b"x")
        batch.add(local, f"data/f/{index}.parquet", (f"f/{index}", {"rows": index}), size=1)

    data_paths = [path for path in api.commits[0].paths if path != publish.MANIFEST_PATH]
    assert data_paths == ["data/f/0.parquet", "data/f/1.parquet"]
    assert sorted(manifest["entries"]) == ["f/0", "f/1"]  # only what actually landed

    batch.flush()

    assert [path for path in api.commits[-1].paths if path != publish.MANIFEST_PATH] == ["data/f/2.parquet"]
    assert sorted(manifest["entries"]) == ["f/0", "f/1", "f/2"]


def test_commit_batch_publishes_the_manifest_in_the_same_commit(tmp_path):
    # The whole bug: the manifest used to be uploaded after the data, and a
    # machine dying in between left months published but unrecorded, which every
    # later run then re-converted and re-uploaded as "novo".
    api = _RecordingApi()
    manifest = publish.empty_manifest()
    batch = publish.CommitBatch(api=api, repo_id="r", manifest=manifest, max_files=1)
    local = tmp_path / "a.parquet"
    local.write_bytes(b"x")

    batch.add(local, "data/f/a.parquet", ("f/a", {"rows": 7}), size=1)

    commit = api.commits[0]
    assert publish.MANIFEST_PATH in commit.paths
    assert _manifest_payload(commit)["entries"]["f/a"] == {"rows": 7}
    # ...and the manifest is not what the message advertises.
    assert commit.message == "Publicar 1 arquivo(s): data/f/a.parquet"


def test_commit_batch_flush_is_a_noop_when_empty():
    class ExplodingApi:
        def create_commit(self, **kwargs):
            raise AssertionError("nao deveria commitar nada")

    publish.CommitBatch(api=ExplodingApi(), repo_id="r", manifest=publish.empty_manifest()).flush()


def test_commit_batch_does_not_record_a_commit_that_failed(tmp_path):
    # The manifest may only claim a month once the commit carrying it landed.
    class FailingApi:
        def create_commit(self, **kwargs):
            raise RuntimeError("hub fora do ar")

    manifest = publish.empty_manifest()
    batch = publish.CommitBatch(api=FailingApi(), repo_id="r", manifest=manifest, max_files=1)
    local = tmp_path / "a.parquet"
    local.write_bytes(b"x")

    with pytest.raises(RuntimeError):
        batch.add(local, "data/f/a.parquet", ("f/a", {}), size=1)

    assert manifest["entries"] == {}
    assert batch.commits == 0


def test_commit_batch_keeps_the_cached_parquet(tmp_path):
    # Converting is the expensive half of a run. The staged files used to be
    # deleted right after the commit, so an interrupted run restarted from zero.
    api = _RecordingApi()
    batch = publish.CommitBatch(api=api, repo_id="r", manifest=publish.empty_manifest(), max_files=1)
    local = tmp_path / "a.parquet"
    local.write_bytes(b"x")

    batch.add(local, "data/f/a.parquet", ("f/a", {}), size=1)

    assert local.exists()


# --------------------------------------------------------------------------
# the local Parquet cache
# --------------------------------------------------------------------------


def _cache_for(tmp_path) -> publish.BuildCache:
    return publish.BuildCache.load(tmp_path / "tmp")


def test_ensure_parquet_converts_then_reuses(tmp_path, make_csv_bytes):
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234", "valor": 1500}]))
    digest = publish.sha256_file(source)
    cache = _cache_for(tmp_path)

    local, record, reused = publish._ensure_parquet(
        _entry(), "perfil_unidades", "2024-06", source, digest, cache
    )

    assert reused is False
    assert local == tmp_path / "tmp" / "data" / "perfil_unidades" / "2024-06.parquet"
    assert record["rows"] == 1
    assert record["parquet_bytes"] == local.stat().st_size
    assert cache.index_path.exists()

    table = pq.read_table(local)
    assert table.column("codigo").to_pylist() == ["01234"]
    assert table.column("periodo_referencia").to_pylist() == ["2024-06"]

    # A second run reads the index, not the source.
    reloaded = _cache_for(tmp_path)
    _, again, reused = publish._ensure_parquet(_entry(), "perfil_unidades", "2024-06", source, digest, reloaded)

    assert reused is True
    assert again["rows"] == 1


def test_ensure_parquet_does_not_reopen_the_source_on_a_cache_hit(tmp_path, make_csv_bytes, monkeypatch):
    # Reusing the cached Parquet is only worth anything if it actually skips the
    # conversion -- reading a multi-hundred-MB XLSX is what costs minutes.
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234"}]))
    digest = publish.sha256_file(source)
    cache = _cache_for(tmp_path)
    publish._ensure_parquet(_entry(), "perfil_unidades", "2024-06", source, digest, cache)

    def explode(*args, **kwargs):
        raise AssertionError("nao deveria reconverter")

    # Both entry points, so this keeps proving something if the conversion is
    # ever routed through the other one again.
    monkeypatch.setattr(publish._reading, "open_resource_chunks", explode)
    monkeypatch.setattr(publish._reading, "read_resource", explode)
    _, _, reused = publish._ensure_parquet(_entry(), "perfil_unidades", "2024-06", source, digest, cache)

    assert reused is True


def test_ensure_parquet_reconverts_when_forced(tmp_path, make_csv_bytes):
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234"}]))
    digest = publish.sha256_file(source)
    cache = _cache_for(tmp_path)
    publish._ensure_parquet(_entry(), "perfil_unidades", "2024-06", source, digest, cache)

    _, _, reused = publish._ensure_parquet(
        _entry(), "perfil_unidades", "2024-06", source, digest, cache, force=True
    )

    assert reused is False


def test_cached_parquet_is_invalidated_by_a_new_source(tmp_path, make_csv_bytes):
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234"}]))
    cache = _cache_for(tmp_path)
    publish._ensure_parquet(
        _entry(), "perfil_unidades", "2024-06", source, publish.sha256_file(source), cache
    )

    assert cache.is_reusable("perfil_unidades", "2024-06", "sha256:outro") is False


def test_cached_parquet_is_invalidated_by_a_recipe_bump(tmp_path, make_csv_bytes, monkeypatch):
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234"}]))
    digest = publish.sha256_file(source)
    cache = _cache_for(tmp_path)
    publish._ensure_parquet(_entry(), "perfil_unidades", "2024-06", source, digest, cache)

    monkeypatch.setattr(publish, "CONVERSION_RECIPE", "v2|dtype=str|compression=zstd|period=str")

    assert cache.is_reusable("perfil_unidades", "2024-06", digest) is False


def test_a_truncated_parquet_is_not_reused(tmp_path, make_csv_bytes):
    # The abrupt-shutdown case the index alone cannot catch: the entry is
    # intact, the bytes are not. Handing that file to the Hub would publish a
    # corrupt month.
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234", "valor": 1500}]))
    digest = publish.sha256_file(source)
    cache = _cache_for(tmp_path)
    local, _, _ = publish._ensure_parquet(_entry(), "perfil_unidades", "2024-06", source, digest, cache)

    with local.open("rb+") as handle:
        handle.truncate(local.stat().st_size // 2)

    assert cache.is_reusable("perfil_unidades", "2024-06", digest) is False


def test_a_deleted_parquet_is_not_reused(tmp_path, make_csv_bytes):
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234"}]))
    digest = publish.sha256_file(source)
    cache = _cache_for(tmp_path)
    local, _, _ = publish._ensure_parquet(_entry(), "perfil_unidades", "2024-06", source, digest, cache)
    local.unlink()

    assert cache.is_reusable("perfil_unidades", "2024-06", digest) is False


def _frames(count: int, *, rows: int = 2) -> list[pd.DataFrame]:
    return [
        pd.DataFrame(
            {
                "periodo_referencia": [pd.Period("2024-06", freq="M")] * rows,
                "codigo": [f"{index:05d}"] * rows,
            }
        )
        for index in range(count)
    ]


def test_write_parquet_never_leaves_a_partial_file_under_the_final_name(tmp_path, monkeypatch):
    # A crash mid-write used to leave a truncated file sitting under the name
    # the cache trusts. Now that the write spans many row groups, the crash is
    # forced on the second one -- the first has really been written by then.
    destination = tmp_path / "data" / "f" / "2024-06.parquet"
    written = 0
    original = pq.ParquetWriter.write_table

    def explode(self, table, *args, **kwargs):
        nonlocal written
        written += 1
        if written > 1:
            raise OSError("disco cheio")
        return original(self, table, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetWriter, "write_table", explode)

    with pytest.raises(OSError, match="disco cheio"):
        publish._write_parquet(iter(_frames(3)), destination)

    assert written == 2  # it really got past the first row group
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_write_parquet_writes_one_row_group_per_chunk(tmp_path):
    # Proof the write is actually incremental: a frame concatenated first and
    # written once would land as a single row group.
    destination = tmp_path / "data" / "f" / "2024-06.parquet"

    rows, columns = publish._write_parquet(iter(_frames(3)), destination)

    assert (rows, columns) == (6, 2)
    assert pq.ParquetFile(destination).num_row_groups == 3


def test_write_parquet_matches_the_whole_frame(tmp_path):
    destination = tmp_path / "data" / "f" / "2024-06.parquet"
    chunks = _frames(3)

    publish._write_parquet(iter(chunks), destination)

    table = pq.read_table(destination)
    assert table.column("codigo").to_pylist() == ["00000", "00000", "00001", "00001", "00002", "00002"]
    # The period column goes out as text, on every chunk and not just the first.
    assert table.column("periodo_referencia").to_pylist() == ["2024-06"] * 6


def test_write_parquet_rejects_a_chunk_whose_schema_drifts(tmp_path):
    destination = tmp_path / "data" / "f" / "2024-06.parquet"
    first, second = _frames(2)
    second = second.rename(columns={"codigo": "outra_coluna"})

    with pytest.raises((ValueError, pa.ArrowInvalid, KeyError)):
        publish._write_parquet(iter([first, second]), destination)

    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_write_json_atomic_replaces_the_previous_file(tmp_path):
    path = tmp_path / "build-index.json"
    publish.write_json_atomic(path, {"entries": {"a": 1}})
    publish.write_json_atomic(path, {"entries": {"a": 2}})

    assert json.loads(path.read_text(encoding="utf-8")) == {"entries": {"a": 2}}
    assert not (tmp_path / "build-index.json.tmp").exists()


def test_a_leftover_temp_index_does_not_shadow_the_good_one(tmp_path):
    root = tmp_path / "tmp"
    root.mkdir()
    publish.write_json_atomic(root / publish.BUILD_INDEX_NAME, {"schema_version": 1, "entries": {"f/2024-06": {}}})
    (root / f"{publish.BUILD_INDEX_NAME}.tmp").write_text("{ truncado", encoding="utf-8")

    cache = publish.BuildCache.load(root)

    assert list(cache.index["entries"]) == ["f/2024-06"]


def test_a_corrupt_index_starts_over_instead_of_failing(tmp_path):
    # Losing the index costs a reconversion, so tolerating it here is right --
    # the opposite of what the manifest may do.
    root = tmp_path / "tmp"
    root.mkdir()
    (root / publish.BUILD_INDEX_NAME).write_text("{ truncado", encoding="utf-8")

    assert publish.BuildCache.load(root).index == publish.empty_index()


def test_prune_removes_only_what_the_manifest_confirms(tmp_path, make_csv_bytes):
    source = tmp_path / "res.csv"
    source.write_bytes(make_csv_bytes([{"codigo": "01234"}]))
    digest = publish.sha256_file(source)
    cache = _cache_for(tmp_path)
    published, _, _ = publish._ensure_parquet(_entry(), "perfil_unidades", "2024-06", source, digest, cache)
    pending, _, _ = publish._ensure_parquet(
        _entry("2024-07"), "perfil_unidades", "2024-07", source, digest, cache
    )
    manifest = {
        "schema_version": 1,
        "entries": {
            "perfil_unidades/2024-06": {"source_sha256": digest, "conversion": publish.CONVERSION_RECIPE}
        },
    }

    files, freed = publish._prune_parquet(cache, manifest)

    assert files == 1
    assert freed > 0
    assert not published.exists()
    assert pending.exists()  # not published yet, so still needed


# --------------------------------------------------------------------------
# reading the Hub's manifest
# --------------------------------------------------------------------------


class _ManifestApi:
    def __init__(self, path: Path | None):
        self.path = path

    def file_exists(self, repo_id, filename, **kwargs):
        return self.path is not None

    def hf_hub_download(self, **kwargs):
        return str(self.path)

    def create_commit(self, **kwargs):
        raise AssertionError("nao deveria publicar nada")


def test_load_manifest_of_a_repo_without_one(tmp_path):
    assert publish._load_manifest(_ManifestApi(None), "r") == publish.empty_manifest()


@pytest.mark.parametrize(
    "payload",
    ["{ truncado", "[]", '{"schema_version": 1}', '{"schema_version": 99, "entries": {}}'],
)
def test_an_untrustworthy_manifest_is_an_error_not_an_empty_one(tmp_path, payload):
    # Falling back to empty_manifest() here would reclassify every published
    # month as "novo" and re-upload the entire dataset.
    path = tmp_path / "manifest.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(publish.ManifestError):
        publish._load_manifest(_ManifestApi(path), "r")


def test_run_refuses_to_publish_when_the_manifest_is_unreadable(tmp_path, monkeypatch):
    import huggingface_hub

    path = tmp_path / "manifest.json"
    path.write_text("{ truncado", encoding="utf-8")
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda *args, **kwargs: _ManifestApi(path))

    code = publish.main(
        ["--familia", "perfil_unidades", "--no-log-file", "--parquet-dir", str(tmp_path / "tmp")]
    )

    assert code == 2


# --------------------------------------------------------------------------
# orphans and reconciliation
# --------------------------------------------------------------------------


def test_find_orphans_lists_published_files_the_manifest_forgot():
    manifest = {"entries": {"beneficios_concedidos/2024-06": {}}}
    repo_files = {
        "data/beneficios_concedidos/2024-06.parquet": 10,
        "data/beneficios_concedidos/2026-07.parquet": 20,
        "data/README.md": 1,
    }

    assert publish.find_orphans(repo_files, manifest) == [
        ("data/beneficios_concedidos/2026-07.parquet", "beneficios_concedidos", "2026-07")
    ]


def test_orphan_report_honours_the_scope_flags(caplog):
    manifest = publish.empty_manifest()
    repo_files = {
        "data/beneficios_concedidos/2026-07.parquet": 1,
        "data/perfil_unidades/2026-07.parquet": 1,
        "data/perfil_unidades/2024-06.parquet": 1,
    }

    orphans = publish._report_orphans(repo_files, manifest, ["perfil_unidades"], ["2026-07"])

    assert orphans == [("data/perfil_unidades/2026-07.parquet", "perfil_unidades", "2026-07")]


def test_reconciliar_adopts_from_the_download_registry_without_downloading(tmp_path, monkeypatch):
    # registry.json keeps a resource's hash even after the source file itself is
    # gone, which is what makes adopting an orphan free.
    entry = _entry()
    registry = {f"perfil_unidades/{_cache._resource_filename(entry)}": "sha256:abc"}
    monkeypatch.setattr(_cache, "_load_registry", lambda root: registry)

    def explode(*args, **kwargs):
        raise AssertionError("nao deveria baixar nada")

    monkeypatch.setattr(_cache, "fetch_resource", explode)

    uploaded = {}

    class FakeApi:
        def upload_file(self, **kwargs):
            uploaded.update(kwargs)

    manifest = publish.empty_manifest()
    path = "data/perfil_unidades/2024-06.parquet"
    catalogs = {"perfil_unidades": SimpleNamespace(entries_by_period={entry.period: entry})}

    publish._reconcile(
        FakeApi(),
        _args(push=True, repo="r"),
        manifest,
        {path: 4096},
        [(path, "perfil_unidades", "2024-06")],
        tmp_path,
        catalogs,
        publish.Plan(),
    )

    adopted = manifest["entries"]["perfil_unidades/2024-06"]
    assert adopted["source_sha256"] == "sha256:abc"
    assert adopted["adopted"] is True
    assert adopted["parquet_bytes"] == 4096
    assert adopted["rows"] is None  # not recoverable without re-reading the source
    assert uploaded["path_in_repo"] == publish.MANIFEST_PATH

    # ...and the point of all this: the month stops looking like "novo".
    should, reason = publish.needs_upload(manifest, "perfil_unidades", "2024-06", "sha256:abc")
    assert should is False
    assert reason == "inalterado"


def test_reconciliar_without_push_only_reports(tmp_path, monkeypatch):
    entry = _entry()
    monkeypatch.setattr(
        _cache, "_load_registry", lambda root: {f"perfil_unidades/{_cache._resource_filename(entry)}": "sha256:abc"}
    )

    class ExplodingApi:
        def upload_file(self, **kwargs):
            raise AssertionError("dry run nao publica")

    manifest = publish.empty_manifest()
    path = "data/perfil_unidades/2024-06.parquet"

    publish._reconcile(
        ExplodingApi(),
        _args(push=False, repo="r"),
        manifest,
        {path: 1},
        [(path, "perfil_unidades", "2024-06")],
        tmp_path,
        {"perfil_unidades": SimpleNamespace(entries_by_period={entry.period: entry})},
        publish.Plan(),
    )

    assert manifest["entries"] == {}


def test_reconciliar_never_downloads_on_a_dry_run(tmp_path, monkeypatch):
    # The script's standing rule is that a dry run costs nothing. Reconciling a
    # month whose source is neither in the registry nor in the cache would mean
    # fetching it, so a dry run reports the cost instead of paying it.
    monkeypatch.setattr(_cache, "_load_registry", lambda root: {})

    def explode(*args, **kwargs):
        raise AssertionError("dry run nao baixa")

    monkeypatch.setattr(_cache, "fetch_resource", explode)

    entry = _entry()
    manifest = publish.empty_manifest()
    path = "data/perfil_unidades/2024-06.parquet"

    publish._reconcile(
        None,
        _args(push=False, repo="r"),
        manifest,
        {path: 1},
        [(path, "perfil_unidades", "2024-06")],
        tmp_path,
        {"perfil_unidades": SimpleNamespace(entries_by_period={entry.period: entry})},
        publish.Plan(),
    )

    assert manifest["entries"] == {}


# --------------------------------------------------------------------------
# --sample and the main loop
# --------------------------------------------------------------------------


def test_sample_mirrors_the_repo_layout_under_the_parquet_dir(tmp_path, make_csv_bytes):
    # The local tree matches path_in_repo so what you inspect is exactly what
    # the Hub would receive -- and warms the cache a later --push reuses.
    cache_root = tmp_path / "cache"
    entry = _entry()
    _seed_source(cache_root, "perfil_unidades", entry, make_csv_bytes([{"codigo": "01234", "valor": 1500}]))
    plan = publish.Plan()
    cache = _cache_for(tmp_path)

    publish._process_one(
        _args(sample=True, parquet_dir=str(cache.root)),
        entry, "perfil_unidades", "2024-06", plan, cache, cache_root, publish.empty_manifest(), None,
    )

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
    cache = _cache_for(tmp_path)

    publish._process_one(
        _args(sample=True, parquet_dir=str(cache.root)),
        _entry(), "perfil_unidades", "2024-06", plan, cache, tmp_path / "cache", publish.empty_manifest(), None,
    )

    assert plan.skipped == [("perfil_unidades", "2024-06")]
    assert plan.uploads == []
    assert not (tmp_path / "tmp").exists()


def test_sample_reuses_an_already_converted_month(tmp_path, make_csv_bytes):
    cache_root = tmp_path / "cache"
    entry = _entry()
    _seed_source(cache_root, "perfil_unidades", entry, make_csv_bytes([{"codigo": "01234"}]))
    cache = _cache_for(tmp_path)
    args = _args(sample=True, parquet_dir=str(cache.root))

    publish._process_one(
        args, entry, "perfil_unidades", "2024-06", publish.Plan(), cache, cache_root,
        publish.empty_manifest(), None,
    )
    written = tmp_path / "tmp" / "data" / "perfil_unidades" / "2024-06.parquet"
    stamp = written.stat().st_mtime_ns

    plan = publish.Plan()
    publish._process_one(
        args, entry, "perfil_unidades", "2024-06", plan, cache, cache_root, publish.empty_manifest(), None,
    )

    assert plan.skipped == [("perfil_unidades", "2024-06")]
    assert written.stat().st_mtime_ns == stamp


def test_sample_redoes_a_month_when_forced(tmp_path, make_csv_bytes):
    cache_root = tmp_path / "cache"
    entry = _entry()
    _seed_source(cache_root, "perfil_unidades", entry, make_csv_bytes([{"codigo": "01234"}]))
    cache = _cache_for(tmp_path)
    publish._process_one(
        _args(sample=True, parquet_dir=str(cache.root)),
        entry, "perfil_unidades", "2024-06", publish.Plan(), cache, cache_root, publish.empty_manifest(), None,
    )

    plan = publish.Plan()
    publish._process_one(
        _args(sample=True, force=True, parquet_dir=str(cache.root)),
        entry, "perfil_unidades", "2024-06", plan, cache, cache_root, publish.empty_manifest(), None,
    )

    assert plan.uploads == [("perfil_unidades", "2024-06", "amostra")]


def test_a_month_that_blows_up_does_not_take_the_run_with_it(tmp_path, monkeypatch):
    # A corrupt XLSX used to abort the whole run at whatever month it hit.
    entries = (_entry("2024-06"), _entry("2024-07"))
    catalog = SimpleNamespace(entries=entries, entries_by_period={e.period: e for e in entries})
    monkeypatch.setattr(publish._catalog, "build_catalog", lambda *args, **kwargs: catalog)
    seen = []

    def flaky(args, entry, family_key, period, plan, *rest):
        seen.append(period)
        if period == "2024-06":
            raise RuntimeError("xlsx corrompido")
        plan.skipped.append((family_key, period))

    monkeypatch.setattr(publish, "_process_one", flaky)

    code = publish.main(
        ["--no-hub", "--familia", "perfil_unidades", "--no-log-file", "--parquet-dir", str(tmp_path / "tmp")]
    )

    assert seen == ["2024-06", "2024-07"]  # the second month still ran
    assert code == 1


def test_no_hub_with_push_is_rejected(capsys):
    code = publish.main(["--no-hub", "--push"])

    assert code == 2
    assert "incompativeis" in capsys.readouterr().err


def test_sample_with_push_is_rejected(capsys):
    code = publish.main(["--sample", "--push"])

    assert code == 2
    assert "incompativeis" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--reconciliar", "--prune-parquet"])
@pytest.mark.parametrize("offline", ["--sample", "--no-hub"])
def test_hub_only_flags_are_rejected_offline(flag, offline, capsys):
    code = publish.main([flag, offline])

    assert code == 2
    assert "precisa consultar o Hub" in capsys.readouterr().err


def test_push_without_any_token_is_rejected(monkeypatch, capsys):
    # The gate used to read os.environ directly, so a user authenticated by
    # `hf auth login` -- token on disk, no env var -- was refused. get_token()
    # covers both, which is why it is what gets stubbed here.
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    code = publish.main(["--push", "--familia", "perfil_unidades"])

    assert code == 2
    err = capsys.readouterr().err
    assert "HF_TOKEN" in err
    assert "hf auth login" in err  # the disk-stored token is a valid source
