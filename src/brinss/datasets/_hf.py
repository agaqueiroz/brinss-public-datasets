"""The Hugging Face mirror: where its files live, and how to read the manifest.

The mirror is written by ``scripts/publish_to_hf.py`` and read by
``_catalog.py``. The layout below is the contract between the two, which is
why it lives here rather than in either of them: a writer and a reader that
disagreed about where a file goes would fail in the least debuggable way
possible, as a month that is published and invisible at the same time.

Files are fetched over plain HTTPS instead of through ``huggingface_hub``. The
repository is public, so nothing is gained by the extra dependency, and going
through a URL means the download reuses ``_cache.fetch_resource`` whole: the
same pooch cache, the same ``registry.json``, the same ``force_download``.
"""

from __future__ import annotations

import requests

from ._families import FAMILIES
from .exceptions import HuggingFaceUnavailableError

BASE_URL = "https://huggingface.co"
REPO_ID = "agaqueiroz/brinss-public-datasets"
MANIFEST_PATH = "manifest.json"
CARD_PATH = "README.md"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 30.0


def path_in_repo(family_key: str, period: str) -> str:
    return f"data/{family_key}/{period}.parquet"


def parse_repo_path(path: str) -> tuple[str, str] | None:
    """The inverse of ``path_in_repo``, or None for anything else in the repo."""
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "data" or not parts[2].endswith(".parquet"):
        return None
    family_key, period = parts[1], parts[2].removesuffix(".parquet")
    return (family_key, period) if family_key in FAMILIES else None


def manifest_key(family_key: str, period: str) -> str:
    return f"{family_key}/{period}"


def resolve_url(path: str, *, repo_id: str = REPO_ID, revision: str = "main") -> str:
    """The direct download URL for one path in the dataset repository."""
    return f"{BASE_URL}/datasets/{repo_id}/resolve/{revision}/{path}"


def fetch_manifest(*, repo_id: str = REPO_ID, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Return the mirror's ``manifest.json``: every published file and its origin.

    Only the period list is needed to build a catalog, and that could be had
    from the repo's file tree instead. The manifest is fetched because it also
    carries the CKAN resource id each Parquet was built from, which is what
    keeps one cached file per resource no matter which source brought it in.
    """
    url = resolve_url(MANIFEST_PATH, repo_id=repo_id)
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HuggingFaceUnavailableError(f"falha ao acessar {url}: {exc}") from exc

    version = payload.get("schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise HuggingFaceUnavailableError(
            f"{MANIFEST_PATH} de '{repo_id}' tem schema_version={version!r}; "
            f"esta versao da biblioteca entende {MANIFEST_SCHEMA_VERSION}."
        )
    if not isinstance(payload.get("entries"), dict):
        raise HuggingFaceUnavailableError(f"{MANIFEST_PATH} de '{repo_id}' nao traz um objeto 'entries'.")
    return payload
