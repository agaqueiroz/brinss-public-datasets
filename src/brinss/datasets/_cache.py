from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import platformdirs
import pooch

from . import _ckan, _log
from ._catalog import ResourceEntry

APP_NAME = "brinss"


def get_cache_root(cache_dir: str | os.PathLike | None = None) -> Path:
    """Resolve the cache directory, in order of precedence:
    explicit argument > ``BRINSS_DATA_HOME`` env var > OS-appropriate user cache dir.
    """
    if cache_dir is not None:
        return Path(cache_dir)
    env_value = os.environ.get("BRINSS_DATA_HOME")
    if env_value:
        return Path(env_value)
    return Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False))


def _registry_path(cache_root: Path) -> Path:
    return cache_root / "registry.json"


def _load_registry(cache_root: Path) -> dict[str, str]:
    path = _registry_path(cache_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_registry(cache_root: Path, registry: dict[str, str]) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    _registry_path(cache_root).write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")


def _sanitize(name: str) -> str:
    keep = "-_.() "
    return "".join(char if char.isalnum() or char in keep else "_" for char in name).strip()


def _resource_filename(entry: ResourceEntry) -> str:
    suffix = Path(entry.url).suffix or (".xlsx" if entry.format.upper() == "XLSX" else "")
    return f"{entry.resource_id}__{_sanitize(entry.resource_name)}{suffix}"


def fetch_resource(
    entry: ResourceEntry,
    *,
    family_key: str,
    cache_dir: str | os.PathLike | None = None,
    force_download: bool = False,
) -> Path:
    """Download (or reuse the cached copy of) one dataset resource, returning its local path.

    The CKAN portal never publishes a hash or size for its resources, so the
    first download of a given file happens with no integrity check. The
    SHA256 is then computed locally and persisted in ``registry.json``; from
    then on pooch verifies that hash on every fetch, which also means it
    will transparently re-download if the government ever replaces a file's
    contents at the same URL without renaming it.
    """
    cache_root = get_cache_root(cache_dir)
    files_dir = cache_root / "files" / family_key
    files_dir.mkdir(parents=True, exist_ok=True)

    filename = _resource_filename(entry)
    key = f"{family_key}/{filename}"
    target_path = files_dir / filename

    registry = _load_registry(cache_root)
    known_hash = registry.get(key)

    if force_download and target_path.exists():
        target_path.unlink()
        registry.pop(key, None)
        _save_registry(cache_root, registry)
        known_hash = None

    fetcher = pooch.create(
        path=files_dir,
        base_url=f"{_ckan.BASE_URL}/",
        registry={filename: known_hash},
        urls={filename: entry.url},
    )

    # pooch logs when it starts downloading but not when it finishes, and says
    # nothing at all on a cache hit. Comparing the mtime around the fetch tells
    # the two apart -- it also catches the case where the hash did not match and
    # pooch silently re-downloaded over the cached copy.
    mtime_before = target_path.stat().st_mtime_ns if target_path.exists() else None
    started_at = time.perf_counter()
    fetched = Path(fetcher.fetch(filename))
    elapsed = time.perf_counter() - started_at

    size = _log.format_bytes(fetched.stat().st_size)
    logger = _log.get_logger()
    if fetched.stat().st_mtime_ns != mtime_before:
        logger.info("Download complete: '%s' (%s) in %s.", filename, size, _log.format_seconds(elapsed))
    else:
        logger.info("Using cached file '%s' (%s).", filename, size)

    if known_hash is None:
        digest = hashlib.sha256(fetched.read_bytes()).hexdigest()
        registry[key] = f"sha256:{digest}"
        _save_registry(cache_root, registry)

    return fetched
