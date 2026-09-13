from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import responses

from brinss.datasets import _cache
from brinss.datasets._catalog import ResourceEntry


def _entry(url: str, *, resource_id: str = "res-1") -> ResourceEntry:
    return ResourceEntry(
        period=pd.Period("2024-06", freq="M"),
        url=url,
        resource_id=resource_id,
        resource_name="Beneficios concedidos junho 2024",
        package_slug="slug",
        format="XLSX",
    )


def test_get_cache_root_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("BRINSS_DATA_HOME", raising=False)

    explicit = tmp_path / "explicit"
    assert _cache.get_cache_root(explicit) == explicit

    env_dir = tmp_path / "env"
    monkeypatch.setenv("BRINSS_DATA_HOME", str(env_dir))
    assert _cache.get_cache_root(None) == env_dir
    assert _cache.get_cache_root(explicit) == explicit  # explicit arg still wins over env var

    monkeypatch.delenv("BRINSS_DATA_HOME", raising=False)
    default_root = _cache.get_cache_root(None)
    assert "brinss" in str(default_root).lower()


@responses.activate
def test_fetch_resource_downloads_and_persists_hash(cache_dir):
    entry = _entry("https://fixtures.test/concedidos/junho-2024.xlsx")
    responses.add(responses.GET, entry.url, body=b"fake-xlsx-bytes", status=200)

    path = _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)

    assert path.exists()
    assert path.read_bytes() == b"fake-xlsx-bytes"

    registry = json.loads((cache_dir / "registry.json").read_text(encoding="utf-8"))
    key = f"beneficios_concedidos/{entry.resource_id}__{entry.resource_name}.xlsx"
    assert key in registry
    assert registry[key].startswith("sha256:")


@responses.activate
def test_registry_holds_the_sha256_of_the_whole_file(cache_dir):
    # A contract, not a detail: the publisher compares this value with the
    # source_sha256 it writes to the mirror's manifest, which hashes the whole
    # source file. The payload spans several read blocks so that hashing only
    # a leading sample, or dropping a trailing partial block, would show here.
    payload = bytes(range(256)) * (3 * 1024 * 4) + b"tail-bytes-17----"
    entry = _entry("https://fixtures.test/concedidos/junho-2024.zip")
    responses.add(responses.GET, entry.url, body=payload, status=200)

    _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)

    registry = json.loads((cache_dir / "registry.json").read_text(encoding="utf-8"))
    key = f"beneficios_concedidos/{entry.resource_id}__{entry.resource_name}.zip"
    assert registry[key] == f"sha256:{hashlib.sha256(payload).hexdigest()}"


@responses.activate
def test_fetch_resource_never_loads_the_download_whole_to_hash_it(cache_dir, monkeypatch):
    # The portal's ZIPs run past 2 GB; read_bytes() would ask for all of it at once.
    entry = _entry("https://fixtures.test/concedidos/junho-2024.xlsx")
    responses.add(responses.GET, entry.url, body=b"fake-xlsx-bytes", status=200)

    def explode(self):
        raise AssertionError("nao deveria carregar o arquivo inteiro na memoria")

    monkeypatch.setattr(Path, "read_bytes", explode)

    path = _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)

    assert path.stat().st_size == len(b"fake-xlsx-bytes")


@responses.activate
def test_fetch_resource_reuses_cache_without_new_request(cache_dir):
    entry = _entry("https://fixtures.test/concedidos/junho-2024.xlsx")
    responses.add(responses.GET, entry.url, body=b"fake-xlsx-bytes", status=200)
    _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)

    responses.reset()  # no responses left registered: a real network call here would fail

    path = _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)
    assert path.read_bytes() == b"fake-xlsx-bytes"


@responses.activate
def test_fetch_resource_force_download_refetches(cache_dir):
    entry = _entry("https://fixtures.test/concedidos/junho-2024.xlsx")
    responses.add(responses.GET, entry.url, body=b"first-version", status=200)
    _cache.fetch_resource(entry, family_key="beneficios_concedidos", cache_dir=cache_dir)

    responses.reset()
    responses.add(responses.GET, entry.url, body=b"second-version", status=200)
    path = _cache.fetch_resource(
        entry, family_key="beneficios_concedidos", cache_dir=cache_dir, force_download=True
    )

    assert path.read_bytes() == b"second-version"
