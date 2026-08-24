#!/usr/bin/env python
"""Convert the INSS datasets to Parquet and publish them to the Hugging Face Hub.

This is a maintainer tool, not part of the distributed package: ``scripts/`` is
outside the ``src/brinss`` module that the build backend packages. It drives the
library's own reading pipeline rather than reimplementing it, so whatever quirk
handling ``brinss.datasets`` grows (banner rows, zip members, encodings) applies
here for free.

One Parquet file per period per family is published:

    data/<family>/<YYYY-MM>.parquet

plus a ``manifest.json`` at the repo root recording, for every published file,
the SHA256 of the *source* file it was built from. That hash is what decides
whether a file needs re-uploading. Hashing the Parquet output instead would be
useless: Parquet is not byte-reproducible across pyarrow versions, so every run
would look like a change.

The manifest travels in the *same commit* as the files it describes. It used to
be uploaded once at the end of the run, which left a window -- minutes wide on a
full load -- where the Hub held files the manifest knew nothing about. A machine
losing power inside that window is not hypothetical: it happened, and left 25
months published but unrecorded, which every later run then re-converted and
re-uploaded as "novo". One commit carrying both makes that state unreachable.

Note the asymmetry the manifest creates: the portal publishes no checksum, so
deciding whether a month changed still requires having the source file locally.
The manifest saves the expensive half (parsing, conversion, upload), not the
download -- and on a machine with a warm cache the download is a no-op anyway.

Converted Parquet is cached under ``tmp/`` (see ``--parquet-dir``) together with
``build-index.json``, which records what was built and from which source. A run
that dies resumes from there instead of re-reading gigabytes of XLSX. The index
is emphatically *not* the manifest: it answers "was this converted here", never
"was this published".

Usage::

    uv run --group publish python scripts/publish_to_hf.py            # dry run
    uv run --group publish python scripts/publish_to_hf.py --sample   # local only
    uv run --group publish python scripts/publish_to_hf.py --push     # for real

``--sample`` converts whatever is already in the download cache, mirroring the
layout the Hub would get. It never downloads and never contacts the Hub, which
makes it the cheap way to eyeball the real output -- and it warms the same cache
a later ``--push`` reuses. A full ``--push`` is 291 files and pulls tens of GB
from the portal.

Pushing needs a write token, either in ``HF_TOKEN`` or stored on disk by
``hf auth login``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from brinss.datasets import _cache, _catalog, _log, _reading
from brinss.datasets._families import FAMILIES
from brinss.datasets.enums import ColumnDtype, XlsxEngine

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARQUET_DIR = REPO_ROOT / "tmp"
DEFAULT_LOG_DIR = REPO_ROOT / "logs"

DEFAULT_REPO_ID = "agaqueiroz/brinss-public-datasets"
MANIFEST_PATH = "manifest.json"
CARD_PATH = "README.md"
MANIFEST_SCHEMA_VERSION = 1
PERIOD_COLUMN = "periodo_referencia"

BUILD_INDEX_NAME = "build-index.json"
BUILD_INDEX_SCHEMA_VERSION = 1

COMPRESSION = "zstd"

# Bumped whenever the conversion itself changes in a way that makes already
# published files stale (column typing, compression, the period column's
# representation). A mismatch forces a rewrite even when the source is
# untouched, which is the only way a recipe change ever reaches the Hub.
CONVERSION_RECIPE = f"v1|dtype={ColumnDtype.STRING.value}|compression={COMPRESSION}|period=str"

_HASH_CHUNK_BYTES = 1024 * 1024

# A batch is flushed once it reaches either bound. The file count keeps the
# commit log readable; the byte cap keeps any single commit from carrying an
# unreasonable payload, which matters on the families whose months run to
# gigabytes. It no longer bounds disk usage -- the converted Parquet is kept on
# purpose now; that job belongs to --prune-parquet and the size warning.
DEFAULT_COMMIT_SIZE = 25
COMMIT_BYTE_CAP = 2 * 1024**3

# Past this, the cache under tmp/ is worth mentioning out loud.
PARQUET_CACHE_WARN_BYTES = 5 * 1024**3

STATUS_OK = "CONCLUIDO COM SUCESSO"
STATUS_FAILED = "CONCLUIDO COM FALHAS"
STATUS_INTERRUPTED = "INTERROMPIDO"

LOGGER = logging.getLogger(f"{_log.LOGGER_NAME}.publish")

# format_bytes is the library's; the script used to carry a near-identical copy.
format_bytes = _log.format_bytes

_LOG_HANDLER: _DurableFileHandler | None = None


class ManifestError(RuntimeError):
    """The Hub's manifest could not be read as a trustworthy record."""


# --------------------------------------------------------------------------
# pure helpers (unit-tested in tests/test_publish_to_hf.py)
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Hash a file in chunks -- the heavier resources run to several GB."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


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


def needs_upload(manifest: dict, family_key: str, period: str, source_sha256: str) -> tuple[bool, str]:
    """Decide whether one period has to be rebuilt, and say why.

    The reason is returned rather than logged here so the dry run and the real
    run can present exactly the same decisions.
    """
    entry = manifest.get("entries", {}).get(manifest_key(family_key, period))
    if entry is None:
        return True, "novo"
    if entry.get("source_sha256") != source_sha256:
        return True, "origem mudou"
    if entry.get("conversion") != CONVERSION_RECIPE:
        return True, "conversao mudou"
    return False, "inalterado"


def empty_manifest() -> dict:
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "entries": {}}


def empty_index() -> dict:
    return {"schema_version": BUILD_INDEX_SCHEMA_VERSION, "entries": {}}


def find_orphans(repo_files: dict[str, int], manifest: dict) -> list[tuple[str, str, str]]:
    """Parquet the repo holds that the manifest does not record.

    With the manifest riding in the same commit as the data this should stay
    empty forever. When it does not, something published outside this script --
    or an older version of it -- and the run says so instead of silently
    re-uploading those months as "novo" on every pass.
    """
    entries = manifest.get("entries", {})
    orphans = []
    for path in sorted(repo_files):
        parsed = parse_repo_path(path)
        if parsed is None:
            continue
        family_key, period = parsed
        if manifest_key(family_key, period) not in entries:
            orphans.append((path, family_key, period))
    return orphans


def normalize_periodos(values: list[str] | None) -> list[str] | None:
    """Normalize the --periodo values, rejecting anything unparseable.

    Round-tripping through ``pd.Period`` also repairs the usual "2024-6" typo,
    which would otherwise match no catalogue entry at all and leave the run
    looking like a legitimate "nothing to do".
    """
    if not values:
        return None
    normalized = []
    for value in values:
        try:
            period = pd.Period(value, freq="M")
        except ValueError as exc:
            raise SystemExit(f"erro: periodo invalido: {value!r} ({exc}).") from exc
        if pd.isna(period):
            raise SystemExit(f"erro: periodo invalido: {value!r}.")
        normalized.append(str(period))
    return normalized


def published_families(manifest: dict) -> list[str]:
    """The families that actually have files in the repo, for the card's configs.

    A config whose glob matches nothing is a broken entry in the Hub's viewer,
    so a partial rollout must not advertise the families it has not reached.
    """
    keys = {key.split("/", 1)[0] for key in manifest.get("entries", {})}
    return sorted(key for key in keys if key in FAMILIES)


def build_dataset_card(family_keys: list[str]) -> str:
    """Render the Hub dataset card, with one viewer config per family.

    Without the ``configs`` block the Hub cannot tell eight unrelated datasets
    apart and the preview shows nothing.
    """
    lines = ["---", "configs:"]
    for key in family_keys:
        lines += [
            f"- config_name: {key}",
            "  data_files:",
            "  - split: train",
            f"    path: data/{key}/*.parquet",
        ]
    lines += [
        "language:",
        "- pt",
        "license: mit",
        "---",
        "",
        "# Datasets abertos do INSS, em Parquet",
        "",
        "Conversão dos datasets publicados em",
        "[dadosabertos.inss.gov.br](https://dadosabertos.inss.gov.br) para Parquet,",
        "gerada com a biblioteca",
        "[brinss-public-datasets](https://github.com/agaqueiroz/brinss-public-datasets).",
        "",
        "Um arquivo por mês, por família: `data/<família>/<AAAA-MM>.parquet`.",
        "",
        "## Tipos das colunas",
        "",
        "Todas as colunas de dados são **texto**. Os códigos do INSS (CID, CBO, CNAE,",
        "código IBGE de município) têm zeros à esquerda que a inferência de tipos",
        'destruiria — `"01234"` viraria `1234`. A coluna `periodo_referencia` é',
        "adicionada pela conversão e traz o mês de referência como `AAAA-MM`.",
        "",
        "## Famílias",
        "",
        "| Config | Descrição |",
        "| --- | --- |",
    ]
    lines += [f"| `{key}` | {FAMILIES[key].title} |" for key in family_keys]
    lines += [
        "",
        "## Procedência",
        "",
        "O `manifest.json` na raiz registra, para cada arquivo, o SHA256 do arquivo",
        "de origem no portal do INSS de que ele foi gerado, além do número de linhas",
        "e da receita de conversão usada. Entradas com `adopted: true` foram",
        "reconciliadas depois do envio: a origem foi conferida, mas a contagem de",
        "linhas daquele arquivo não foi recalculada.",
        "",
        "<!-- Gerado por scripts/publish_to_hf.py. Edições manuais são preservadas:",
        "     o script só reescreve este arquivo com --update-card. -->",
        "",
    ]
    return "\n".join(lines)


def to_publishable(frame: pd.DataFrame) -> pd.DataFrame:
    """Make a frame safe to read outside pandas.

    ``periodo_referencia`` arrives as a ``pandas.Period``, which Parquet stores
    as a pandas-specific extension type over a raw month ordinal (2024-06 is
    written as 653). Pandas round-trips it, but the Hub's viewer, DuckDB and
    polars all see a meaningless integer -- so it goes out as "YYYY-MM" text.

    The copy is deliberately shallow: a deep one would duplicate every column
    of a frame that already runs to gigabytes on the heavy families, doubling
    peak memory to rewrite a single column. Under pandas' copy-on-write the
    caller's frame is still left untouched.
    """
    if PERIOD_COLUMN in frame.columns:
        frame = frame.copy(deep=False)
        frame[PERIOD_COLUMN] = frame[PERIOD_COLUMN].astype(str)
    return frame


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# durable writes -- an abrupt shutdown is the failure model, not an exception
# --------------------------------------------------------------------------


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON so an interrupted run never leaves a half-file behind.

    ``os.replace`` alone is not enough. It makes the *rename* atomic, but the
    bytes can still be sitting in the page cache when the machine loses power,
    and what survives is a truncated file under the final name. The fsync is
    what turns "renamed" into "durable".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    with partial.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


def _write_parquet(frame: pd.DataFrame, destination: Path) -> None:
    """Convert to Parquet via a .part file, so the final name is never truncated.

    A crash inside ``to_parquet`` would otherwise leave a partial file under the
    name the cache trusts, and the next run would hand it to the Hub as a
    finished month.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        to_publishable(frame).to_parquet(partial, engine="pyarrow", compression=COMPRESSION, index=False)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, destination)


# --------------------------------------------------------------------------
# the local Parquet cache
# --------------------------------------------------------------------------


@dataclass
class BuildCache:
    """The converted Parquet kept under ``tmp/``, plus the index describing it.

    Conversion is the expensive half of a run -- reading a multi-hundred-MB
    XLSX with openpyxl takes minutes -- and it used to be thrown away twice
    over: deleted right after each commit, and again with the staging
    directory. Keeping it means an interrupted run resumes instead of restarts.

    The index is what makes the files on disk trustworthy. A Parquet whose name
    merely exists proves nothing after a crash; one whose recorded source hash,
    conversion recipe *and byte count* still match is the same file this script
    would produce right now.
    """

    root: Path
    index: dict

    @classmethod
    def load(cls, root: Path | str) -> BuildCache:
        root = Path(root)
        return cls(root=root, index=_load_build_index(root / BUILD_INDEX_NAME))

    @property
    def index_path(self) -> Path:
        return self.root / BUILD_INDEX_NAME

    @property
    def data_root(self) -> Path:
        return self.root / "data"

    def path_for(self, family_key: str, period: str) -> Path:
        return self.root / path_in_repo(family_key, period)

    def entry(self, family_key: str, period: str) -> dict | None:
        return self.index.get("entries", {}).get(manifest_key(family_key, period))

    def is_reusable(self, family_key: str, period: str, source_sha256: str) -> bool:
        """Whether the cached Parquet can stand in for a fresh conversion."""
        entry = self.entry(family_key, period)
        if entry is None:
            return False
        if entry.get("source_sha256") != source_sha256 or entry.get("conversion") != CONVERSION_RECIPE:
            return False
        local = self.path_for(family_key, period)
        if not local.exists():
            return False
        # The size check is what catches a file truncated by a power cut: the
        # index entry is intact, the bytes are not.
        return local.stat().st_size == entry.get("parquet_bytes")

    def record(self, family_key: str, period: str, entry: dict) -> None:
        self.index.setdefault("entries", {})[manifest_key(family_key, period)] = entry
        write_json_atomic(self.index_path, self.index)

    def total_bytes(self) -> int:
        if not self.data_root.exists():
            return 0
        return sum(path.stat().st_size for path in self.data_root.rglob("*.parquet"))


def _load_build_index(path: Path) -> dict:
    """Read the build index, falling back to an empty one when it is unusable.

    Tolerating corruption is right *here* and wrong for the manifest: losing the
    index costs a reconversion, while misreading the manifest as empty would
    re-upload all 291 months.
    """
    if not path.exists():
        return empty_index()
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        LOGGER.warning("indice de build ilegivel (%s); recomecando vazio: %s", path, exc)
        return empty_index()
    if not isinstance(index, dict) or not isinstance(index.get("entries"), dict):
        LOGGER.warning("indice de build sem 'entries' (%s); recomecando vazio.", path)
        return empty_index()
    return index


def _ensure_parquet(
    entry,
    family_key: str,
    period: str,
    source: Path,
    digest: str,
    cache: BuildCache,
    *,
    force: bool = False,
) -> tuple[Path, dict, bool]:
    """Return the cached Parquet for one month, converting it only if needed.

    The third element says whether the file was reused. On a reuse the row and
    byte counts come from the index, so a re-upload never has to reopen a frame
    it already converted.
    """
    local = cache.path_for(family_key, period)
    if not force and cache.is_reusable(family_key, period, digest):
        LOGGER.info("  cache %s/%s (parquet ja convertido)", family_key, period)
        return local, cache.entry(family_key, period), True

    started_at = time.perf_counter()
    frame = _reading.read_resource(
        source, entry, columns=None, engine=XlsxEngine.OPENPYXL, dtype=ColumnDtype.STRING
    )
    _write_parquet(frame, local)
    record = {
        "source_sha256": digest,
        "source_url": entry.url,
        "resource_id": entry.resource_id,
        "rows": len(frame),
        "columns": len(frame.columns),
        "parquet_bytes": local.stat().st_size,
        "conversion": CONVERSION_RECIPE,
        "built_at": _utcnow(),
    }
    del frame
    # Parquet first, index second. The only inconsistency a crash can leave is
    # "file is there, index does not know", which costs one reconversion. The
    # other order leaves the index vouching for a file that may be truncated.
    cache.record(family_key, period, record)
    LOGGER.info(
        "  gera  %s (%s linhas, %s, %s)",
        local,
        f"{record['rows']:,}",
        format_bytes(record["parquet_bytes"]),
        _log.format_seconds(time.perf_counter() - started_at),
    )
    return local, record, False


def _manifest_entry(record: dict) -> dict:
    """The manifest's view of a build record: everything but the local detail."""
    return {key: value for key, value in record.items() if key != "built_at"}


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


class _DurableFileHandler(logging.FileHandler):
    """A file handler whose checkpoint lines survive the crash that caused them.

    ``StreamHandler.emit`` already flushes every record, which is enough when
    the process is killed. It is not enough when the machine loses power: the
    bytes are in the page cache, not on the disk. The lines that answer "where
    did it stop" ask for an fsync explicitly -- doing it for every record would
    make the log cost more than the work it describes.
    """

    def sync(self) -> None:
        if self.stream is None:
            return
        self.flush()
        try:
            os.fsync(self.stream.fileno())
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            pass


def _setup_logging(args: argparse.Namespace) -> Path | None:
    """Wire the script into the library's logger and open the run's log file.

    ``brinss.publish`` propagates to ``brinss``, which ``_log.get_logger`` has
    already given a stderr handler -- so the script's messages and the library's
    ("Downloading...", "Using cached file...") share one stream in one format.
    The file handler is attached to ``brinss`` and ``pooch`` for the same
    reason: what went wrong is usually a download or a read, not this script.
    """
    global _LOG_HANDLER

    _log.get_logger()  # installs the stderr handler once
    console_level = logging.DEBUG if args.verbose else logging.INFO
    handler: _DurableFileHandler | None = None
    path: Path | None = None

    if not args.no_log_file:
        default_name = f"publish-{time.strftime('%Y%m%d-%H%M%S')}.log"
        path = Path(args.log_file) if args.log_file else Path(args.log_dir) / default_name
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _DurableFileHandler(path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))

    for name in (_log.LOGGER_NAME, "pooch"):
        logger = logging.getLogger(name)
        for existing in logger.handlers:
            existing.setLevel(console_level)
        if handler is not None:
            logger.addHandler(handler)
        # DEBUG on the logger, INFO on the console handler: the file gets the
        # detail without the terminal being buried in it.
        logger.setLevel(logging.DEBUG if handler is not None else console_level)

    _LOG_HANDLER = handler
    return path


def _checkpoint(message: str, *args) -> None:
    """Log a line that has to be readable after a power cut."""
    LOGGER.info(message, *args)
    if _LOG_HANDLER is not None:
        _LOG_HANDLER.sync()


def _log_header(
    args: argparse.Namespace,
    *,
    cache_root: Path,
    family_keys: list[str],
    requested_periods: list[str] | None,
    log_path: Path | None,
) -> None:
    mode = "sample" if args.sample else ("push" if args.push else "dry-run")
    if args.reconciliar:
        mode += "+reconciliar"
    LOGGER.info("=" * 72)
    LOGGER.info("publish_to_hf  inicio %s  modo=%s", _utcnow(), mode)
    LOGGER.info("  repo             %s", "(nenhum)" if args.sample or args.no_hub else args.repo)
    LOGGER.info("  familias         %s", ", ".join(family_keys))
    LOGGER.info("  periodos         %s", ", ".join(requested_periods) if requested_periods else "todos")
    LOGGER.info("  cache downloads  %s", cache_root)
    LOGGER.info("  cache parquet    %s", args.parquet_dir)
    LOGGER.info("  receita          %s", CONVERSION_RECIPE)
    LOGGER.info("  log              %s", log_path or "(sem arquivo)")
    LOGGER.info("=" * 72)


# --------------------------------------------------------------------------
# planning and execution
# --------------------------------------------------------------------------


@dataclass
class Plan:
    uploads: list[tuple[str, str, str]] = field(default_factory=list)  # family, period, reason
    skipped: list[tuple[str, str]] = field(default_factory=list)  # family, period
    failures: list[tuple[str, str, str]] = field(default_factory=list)  # family, period, error
    reconciled: list[tuple[str, str]] = field(default_factory=list)  # family, period
    written_bytes: int = 0
    reused_bytes: int = 0
    last_item: str | None = None
    status: str = STATUS_OK


@dataclass
class CommitBatch:
    """Groups Parquet files, and the manifest describing them, into one commit.

    A file-per-commit first load is roughly 300 sequential commits: slow, hard
    on the Hub's rate limiting, and it buries the repository history.

    The manifest goes in the *same* commit as the data. Uploading it afterwards
    left a window where the Hub held files the manifest did not record, and an
    abrupt shutdown inside that window is what stranded 25 months as permanent
    "novo". One commit carrying both makes the inconsistent state unreachable;
    the in-memory manifest is only updated once the commit has landed.
    """

    api: object
    repo_id: str
    manifest: dict
    max_files: int = DEFAULT_COMMIT_SIZE
    max_bytes: int = COMMIT_BYTE_CAP
    operations: list = field(default_factory=list)
    pending: list[tuple[str, dict]] = field(default_factory=list)
    pending_bytes: int = 0
    commits: int = 0

    def add(self, local: Path, target: str, manifest_entry: tuple[str, dict], size: int) -> None:
        from huggingface_hub import CommitOperationAdd

        self.operations.append(CommitOperationAdd(path_in_repo=target, path_or_fileobj=str(local)))
        self.pending.append(manifest_entry)
        self.pending_bytes += size
        if len(self.operations) >= self.max_files or self.pending_bytes >= self.max_bytes:
            self.flush()

    def flush(self) -> None:
        if not self.operations:
            return
        from huggingface_hub import CommitOperationAdd

        # Read before the manifest operation joins them, or the message names it.
        targets = [operation.path_in_repo for operation in self.operations]
        merged = {**self.manifest.get("entries", {}), **dict(self.pending)}
        payload = {"schema_version": MANIFEST_SCHEMA_VERSION, "entries": merged}
        operations = [
            *self.operations,
            CommitOperationAdd(
                path_in_repo=MANIFEST_PATH,
                path_or_fileobj=json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
            ),
        ]
        info = self.api.create_commit(
            repo_id=self.repo_id,
            repo_type="dataset",
            operations=operations,
            commit_message=_commit_message(targets),
        )
        # Only now that the commit landed may the manifest claim these months.
        self.manifest["entries"] = merged
        self.commits += 1
        _checkpoint(
            "commit %d: %d arquivo(s) + manifesto, %s%s",
            self.commits,
            len(targets),
            format_bytes(self.pending_bytes),
            _commit_ref(info),
        )
        self.operations.clear()
        self.pending.clear()
        self.pending_bytes = 0


def _commit_message(targets: list[str]) -> str:
    message = f"Publicar {len(targets)} arquivo(s): {targets[0]}"
    if len(targets) > 1:
        message += f" ... {targets[-1]}"
    return message


def _commit_ref(info) -> str:
    oid = getattr(info, "oid", None)
    return f" ({oid[:8]})" if isinstance(oid, str) else ""


def _load_manifest(api, repo_id: str) -> dict:
    """Read the Hub's manifest, refusing to guess when it cannot be trusted.

    An unreadable manifest must never degrade into an empty one: that would
    reclassify every published month as "novo" and re-upload the whole dataset.
    ``empty_manifest()`` is returned only for the two cases that positively
    establish there is nothing published yet.

    The download is forced past the local ``huggingface_hub`` cache -- the file
    is tiny, and re-reading a blob that a crash may have left half-written is
    not a risk worth taking.
    """
    from huggingface_hub.errors import RepositoryNotFoundError

    try:
        if not api.file_exists(repo_id, MANIFEST_PATH, repo_type="dataset"):
            return empty_manifest()
        local = api.hf_hub_download(
            repo_id=repo_id, filename=MANIFEST_PATH, repo_type="dataset", force_download=True
        )
    except RepositoryNotFoundError:
        return empty_manifest()

    try:
        manifest = json.loads(Path(local).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{MANIFEST_PATH} do repositorio esta ilegivel: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), dict):
        raise ManifestError(f"{MANIFEST_PATH} do repositorio nao traz um objeto 'entries'.")
    version = manifest.get("schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"{MANIFEST_PATH} tem schema_version={version!r}; este script entende {MANIFEST_SCHEMA_VERSION}."
        )
    return manifest


def _repo_parquet_files(api, repo_id: str) -> dict[str, int]:
    """Every published Parquet and its size, for orphan detection and pruning."""
    from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError

    try:
        tree = api.list_repo_tree(repo_id, path_in_repo="data", repo_type="dataset", recursive=True)
        return {item.path: getattr(item, "size", 0) for item in tree if item.path.endswith(".parquet")}
    except (RepositoryNotFoundError, EntryNotFoundError):
        return {}


def _cached_source(entry, family_key: str, cache_root: Path) -> Path | None:
    """The already-downloaded source file, or None if it is not in the cache.

    This borrows _cache's naming rule so a dry run stays cheap. Without it,
    previewing the plan would have to download every resource -- tens of GB
    across the heavy families -- purely to hash bytes it then throws away.

    tests/test_publish_to_hf.py pins this against the real _cache helpers, so a
    rename there fails the suite instead of silently reporting every cached
    month as missing.
    """
    path = cache_root / "files" / family_key / _cache._resource_filename(entry)
    return path if path.exists() else None


def _source_sha(
    entry, family_key: str, cache_root: Path, registry: dict[str, str], *, allow_download: bool
) -> tuple[str | None, str]:
    """The source's SHA256 and where it came from, cheapest source first.

    ``registry.json`` is the cheap win: the library records a resource's hash on
    first download and keeps it even after the file itself is deleted, so a
    month whose source is long gone can still be identified for free.
    """
    known = registry.get(f"{family_key}/{_cache._resource_filename(entry)}")
    if known:
        return known, "registry"
    cached = _cached_source(entry, family_key, cache_root)
    if cached is not None:
        return sha256_file(cached), "cache"
    if not allow_download:
        return None, "indisponivel"
    fetched = _cache.fetch_resource(entry, family_key=family_key, cache_dir=cache_root)
    return sha256_file(fetched), "download"


def _prune_parquet(cache: BuildCache, manifest: dict) -> tuple[int, int]:
    """Delete cached Parquet the Hub demonstrably already has.

    Only files whose manifest entry matches the index on both source hash and
    conversion recipe go: anything else is either unpublished or stale, and
    deleting it would throw away work the next run still needs.
    """
    published = manifest.get("entries", {})
    files = freed = 0
    for key, record in list(cache.index.get("entries", {}).items()):
        entry = published.get(key)
        if entry is None or entry.get("source_sha256") != record.get("source_sha256"):
            continue
        if entry.get("conversion") != CONVERSION_RECIPE or record.get("conversion") != CONVERSION_RECIPE:
            continue
        family_key, _, period = key.partition("/")
        local = cache.path_for(family_key, period)
        if not local.exists():
            continue
        freed += local.stat().st_size
        local.unlink()
        files += 1
    return files, freed


def _reconcile(
    api,
    args: argparse.Namespace,
    manifest: dict,
    repo_files: dict[str, int],
    orphans: list[tuple[str, str, str]],
    cache_root: Path,
    catalogs: dict,
    plan: Plan,
) -> None:
    """Adopt Parquet already on the Hub into the manifest, without re-uploading.

    What the entry cannot recover is the row count: that would mean reading the
    source back, which is the expensive half this exists to skip. ``adopted:
    true`` marks those entries so a later audit can force a proper rewrite.

    The assumption worth stating out loud: this trusts that the published file
    was built from the source that is current *now*. If the portal replaced the
    file between the upload and this reconciliation, the entry is born wrong and
    that month will never refresh itself.

    Downloading to recover a hash only happens under ``--push``. A dry run keeps
    the script's standing rule -- it never fetches -- and instead reports which
    months could be adopted for free and which would cost a download.
    """
    registry = _cache._load_registry(cache_root)
    adopted: dict[str, dict] = {}
    would_download = 0

    for path, family_key, period in orphans:
        catalog = catalogs.get(family_key)
        entry = catalog.entries_by_period.get(pd.Period(period, freq="M")) if catalog else None
        if entry is None:
            LOGGER.warning("  orfao %s: sem recurso correspondente no catalogo; ignorado.", path)
            continue
        digest, origin = _source_sha(entry, family_key, cache_root, registry, allow_download=args.push)
        if digest is None:
            would_download += 1
            LOGGER.info("  orfao %s/%s: exigiria baixar a origem para reconciliar.", family_key, period)
            continue
        adopted[manifest_key(family_key, period)] = {
            "source_sha256": digest,
            "source_url": entry.url,
            "resource_id": entry.resource_id,
            "rows": None,
            "columns": None,
            "parquet_bytes": repo_files.get(path),
            "conversion": CONVERSION_RECIPE,
            "adopted": True,
        }
        LOGGER.info("  adota %s/%s (sha via %s)", family_key, period, origin)

    if would_download:
        LOGGER.info("reconciliar: %d arquivo(s) exigem baixar a origem; repita com --push.", would_download)
    if not adopted:
        LOGGER.info("reconciliar: nada a adotar.")
        return
    if not args.push:
        LOGGER.info("reconciliar: %d arquivo(s) seriam adotados (dry run).", len(adopted))
        return

    merged = {**manifest.get("entries", {}), **adopted}
    payload = {"schema_version": MANIFEST_SCHEMA_VERSION, "entries": merged}
    _upload_json(api, args.repo, MANIFEST_PATH, payload, f"Reconciliar {len(adopted)} arquivo(s) ja publicados")
    manifest["entries"] = merged
    plan.reconciled.extend(tuple(key.split("/", 1)) for key in sorted(adopted))
    _checkpoint("reconciliar: %d arquivo(s) adotados no manifesto.", len(adopted))


def _process_one(
    args: argparse.Namespace,
    entry,
    family_key: str,
    period: str,
    plan: Plan,
    cache: BuildCache,
    cache_root: Path,
    manifest: dict,
    batch: CommitBatch | None,
) -> None:
    """Handle one month: decide, convert if needed, and stage it for a commit."""
    target = path_in_repo(family_key, period)
    source = _cached_source(entry, family_key, cache_root)

    if args.sample:
        # --sample never downloads: that is the whole reason it exists, since a
        # full run pulls tens of GB from the portal.
        if source is None:
            plan.skipped.append((family_key, period))
            LOGGER.info("  skip  %s/%s (nao esta em cache)", family_key, period)
            return
        digest = sha256_file(source)
        _, record, reused = _ensure_parquet(entry, family_key, period, source, digest, cache, force=args.force)
        if reused:
            plan.skipped.append((family_key, period))
            plan.reused_bytes += record["parquet_bytes"]
        else:
            plan.uploads.append((family_key, period, "amostra"))
            plan.written_bytes += record["parquet_bytes"]
        return

    if source is None and not args.push:
        # Cannot know whether it changed without the bytes, and a dry run is not
        # worth a multi-GB download.
        plan.uploads.append((family_key, period, "fonte nao baixada"))
        LOGGER.info("  PLAN  %s (fonte ainda nao baixada)", target)
        return
    if source is None:
        source = _cache.fetch_resource(entry, family_key=family_key, cache_dir=cache_root)
    digest = sha256_file(source)

    should, reason = needs_upload(manifest, family_key, period, digest)
    if args.force and not should:
        should, reason = True, "forcado"
    if not should:
        plan.skipped.append((family_key, period))
        LOGGER.info("  skip  %s/%s (%s)", family_key, period, reason)
        return

    plan.uploads.append((family_key, period, reason))
    if not args.push:
        LOGGER.info("  PLAN  %s (%s)", target, reason)
        return

    local, record, reused = _ensure_parquet(entry, family_key, period, source, digest, cache, force=args.force)
    size = record["parquet_bytes"]
    if reused:
        plan.reused_bytes += size
    else:
        plan.written_bytes += size
    LOGGER.info("  push  %s (%s linhas, %s, %s)", target, f"{record['rows']:,}", format_bytes(size), reason)
    batch.add(local, target, (manifest_key(family_key, period), _manifest_entry(record)), size)


def _has_token() -> bool:
    # get_token() walks several sources: an OIDC exchange when HF_OIDC_RESOURCE
    # is set (Trusted Publishers in CI), then HF_TOKEN, then
    # HUGGING_FACE_HUB_TOKEN, then the token file written by `hf auth login`,
    # then Colab secrets. Reading the environment alone would reject a
    # perfectly good CLI session.
    from huggingface_hub import get_token

    return bool(get_token())


def _validate(args: argparse.Namespace) -> int:
    """Reject impossible flag combinations before anything is set up.

    This runs ahead of the logging setup on purpose: a run that cannot start
    should not leave a log file behind claiming it did.
    """
    if args.no_hub and args.push:
        print("erro: --no-hub e --push sao incompativeis (publicar exige ler o manifesto).", file=sys.stderr)
        return 2
    if args.sample and args.push:
        print("erro: --sample e --push sao incompativeis (--sample so escreve localmente).", file=sys.stderr)
        return 2
    for flag, requested in (("--reconciliar", args.reconciliar), ("--prune-parquet", args.prune_parquet)):
        if requested and (args.sample or args.no_hub):
            print(f"erro: {flag} precisa consultar o Hub (incompativel com --sample e --no-hub).", file=sys.stderr)
            return 2
    if args.push and not _has_token():
        print(
            "erro: --push exige um token de escrita. Defina HF_TOKEN ou rode `hf auth login`.",
            file=sys.stderr,
        )
        return 2
    return 0


def run(args: argparse.Namespace) -> int:
    invalid = _validate(args)
    if invalid:
        return invalid

    started_at = time.perf_counter()
    log_path = _setup_logging(args)

    requested_periods = normalize_periodos(args.periodo)
    cache_root = _cache.get_cache_root(args.cache_dir)
    family_keys = args.familia or list(FAMILIES)
    _log_header(
        args,
        cache_root=cache_root,
        family_keys=family_keys,
        requested_periods=requested_periods,
        log_path=log_path,
    )

    cache = BuildCache.load(args.parquet_dir)
    api = None
    manifest = empty_manifest()
    repo_files: dict[str, int] = {}
    catalogs: dict[str, _catalog.DatasetCatalog] = {}

    if not args.no_hub and not args.sample:
        from huggingface_hub import HfApi

        api = HfApi()
        if args.push and args.create_repo:
            api.create_repo(args.repo, repo_type="dataset", exist_ok=True)
        try:
            manifest = _load_manifest(api, args.repo)
        except ManifestError as exc:
            LOGGER.error("%s", exc)
            LOGGER.error("nada foi publicado: um manifesto ilegivel faria o script reenviar o dataset inteiro.")
            return 2
        repo_files = _repo_parquet_files(api, args.repo)
        LOGGER.info(
            "manifesto: %d entrada(s); repositorio: %d parquet.", len(manifest["entries"]), len(repo_files)
        )

    plan = Plan()
    matched_periods: set[str] = set()
    batch = CommitBatch(api=api, repo_id=args.repo, manifest=manifest, max_files=args.commit_size)

    if args.prune_parquet:
        files, freed = _prune_parquet(cache, manifest)
        LOGGER.info("prune: %d parquet removido(s), %s liberados.", files, format_bytes(freed))

    try:
        for family_key in family_keys:
            catalogs[family_key] = _catalog.build_catalog(
                FAMILIES[family_key], cache_dir=cache_root, force_refresh=args.force_refresh
            )

        orphans = _report_orphans(repo_files, manifest, family_keys, requested_periods)
        if args.reconciliar:
            _reconcile(api, args, manifest, repo_files, orphans, cache_root, catalogs, plan)

        for family_key in family_keys:
            for entry in catalogs[family_key].entries:
                period = str(entry.period)
                if requested_periods is not None:
                    if period not in requested_periods:
                        continue
                    matched_periods.add(period)

                if args.limite and len(plan.uploads) >= args.limite:
                    LOGGER.info("limite de %d arquivos atingido; parando.", args.limite)
                    return _finish(
                        api, args, manifest, plan, batch, matched_periods,
                        requested_periods, cache, log_path, started_at,
                    )

                plan.last_item = f"{family_key}/{period}"
                # Logged *before* the work, not after: on a power cut the last
                # line of the log has to name the month that was in flight, not
                # the last one that finished.
                _checkpoint("iniciando %s", plan.last_item)
                try:
                    _process_one(args, entry, family_key, period, plan, cache, cache_root, manifest, batch)
                except Exception as exc:
                    LOGGER.exception("falha em %s/%s", family_key, period)
                    plan.failures.append((family_key, period, f"{type(exc).__name__}: {exc}"))
                    plan.status = STATUS_FAILED

            if args.push:
                # Keep every commit within one family, so the message reads well.
                batch.flush()
    except KeyboardInterrupt:
        plan.status = STATUS_INTERRUPTED
        LOGGER.warning("interrompido; publicando o que ja foi convertido.")
    except Exception:
        plan.status = STATUS_FAILED
        LOGGER.exception("erro nao tratado; encerrando pelo caminho normal para nao perder o lote pendente")

    return _finish(
        api, args, manifest, plan, batch, matched_periods, requested_periods, cache, log_path, started_at
    )


def _report_orphans(
    repo_files: dict[str, int],
    manifest: dict,
    family_keys: list[str],
    requested_periods: list[str] | None,
) -> list[tuple[str, str, str]]:
    orphans = [
        item
        for item in find_orphans(repo_files, manifest)
        if item[1] in family_keys and (requested_periods is None or item[2] in requested_periods)
    ]
    if not orphans:
        return orphans
    LOGGER.warning(
        "%d arquivo(s) no repositorio sem entrada no manifesto; serao reenviados como 'novo'.", len(orphans)
    )
    for path, _, _ in orphans[:10]:
        LOGGER.warning("  orfao %s", path)
    if len(orphans) > 10:
        LOGGER.warning("  ... e mais %d.", len(orphans) - 10)
    LOGGER.warning("use --reconciliar para adota-los no manifesto sem reconverter nem reenviar.")
    return orphans


def _finish(
    api,
    args: argparse.Namespace,
    manifest: dict,
    plan: Plan,
    batch: CommitBatch,
    matched_periods: set[str],
    requested_periods: list[str] | None,
    cache: BuildCache,
    log_path: Path | None,
    started_at: float,
) -> int:
    """Land the last batch, refresh the card, then summarize the run."""
    if args.push:
        try:
            batch.flush()
        except Exception:
            LOGGER.exception("falha ao publicar o ultimo lote")
            plan.failures.append(("-", "-", "falha no commit final"))
            plan.status = STATUS_FAILED
        if batch.commits:
            try:
                _maybe_upload_card(api, args.repo, manifest, update=args.update_card)
            except Exception:
                LOGGER.exception("falha ao atualizar o dataset card (os dados ja estao publicados)")

    unmatched = sorted(set(requested_periods or []) - matched_periods)
    if unmatched:
        LOGGER.error("nenhum recurso encontrado para: %s.", ", ".join(unmatched))
        if plan.status == STATUS_OK:
            plan.status = STATUS_FAILED

    _log_summary(args, plan, batch, cache, log_path, started_at)
    return 1 if (plan.failures or unmatched) else 0


def _log_summary(
    args: argparse.Namespace,
    plan: Plan,
    batch: CommitBatch,
    cache: BuildCache,
    log_path: Path | None,
    started_at: float,
) -> None:
    verb = "gerados" if args.sample else ("enviados" if args.push else "a enviar")
    LOGGER.info("-" * 72)
    _checkpoint("%s", plan.status)
    LOGGER.info("  %-16s %d", verb, len(plan.uploads))
    LOGGER.info("  %-16s %d", "pulados", len(plan.skipped))
    LOGGER.info("  %-16s %d", "falhas", len(plan.failures))
    if plan.reconciled:
        LOGGER.info("  %-16s %d", "reconciliados", len(plan.reconciled))
    LOGGER.info(
        "  %-16s %s convertidos, %s reaproveitados do cache",
        "bytes",
        format_bytes(plan.written_bytes),
        format_bytes(plan.reused_bytes),
    )
    if args.push:
        LOGGER.info("  %-16s %d", "commits", batch.commits)
    LOGGER.info("  %-16s %s", "duracao", _log.format_seconds(time.perf_counter() - started_at))
    LOGGER.info("  %-16s %s", "parou em", plan.last_item or "(nada processado)")
    for family_key, period, error in plan.failures:
        LOGGER.info("  falha    %s/%s: %s", family_key, period, error)

    total = cache.total_bytes()
    LOGGER.info("  %-16s %s em %s", "cache parquet", format_bytes(total), cache.data_root)
    if total >= PARQUET_CACHE_WARN_BYTES:
        LOGGER.warning(
            "o cache de parquet ja ocupa %s; use --prune-parquet para apagar o que ja esta publicado.",
            format_bytes(total),
        )
    if log_path is not None:
        LOGGER.info("  %-16s %s", "log", log_path)
    LOGGER.info("-" * 72)

    if not args.push and not args.sample and plan.uploads:
        LOGGER.info("nada foi enviado (dry run). repita com --push para publicar.")


def _upload_json(api, repo_id: str, path: str, payload: dict, message: str) -> None:
    api.upload_file(
        path_or_fileobj=json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        path_in_repo=path,
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=message,
    )


def _maybe_upload_card(api, repo_id: str, manifest: dict, *, update: bool) -> None:
    """Write the dataset card, but never clobber one edited on the Hub.

    The card is generated, so republishing it on every run would silently throw
    away any citation, example or note added through the web UI. It is written
    when the repo has none, and otherwise only on an explicit --update-card.
    """
    if not update and api.file_exists(repo_id, CARD_PATH, repo_type="dataset"):
        return
    families = published_families(manifest) or sorted(FAMILIES)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / CARD_PATH
        local.write_text(build_dataset_card(families), encoding="utf-8")
        api.upload_file(
            path_or_fileobj=str(local),
            path_in_repo=CARD_PATH,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message="Atualizar dataset card",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Converte os datasets do INSS para Parquet e publica no Hugging Face.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", default=DEFAULT_REPO_ID, help=f"repositorio no Hub (padrao: {DEFAULT_REPO_ID})")
    parser.add_argument(
        "--push",
        action="store_true",
        help="publica de fato; sem esta flag o script so mostra o plano (dry run)",
    )
    parser.add_argument(
        "--familia",
        action="append",
        choices=sorted(FAMILIES),
        help="restringe a uma familia (repetivel; padrao: todas)",
    )
    parser.add_argument(
        "--periodo",
        action="append",
        help="restringe a um mes AAAA-MM (repetivel; padrao: todos do catalogo)",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="converte para parquet so o que ja esta em cache, gravando localmente e sem tocar no Hub",
    )
    parser.add_argument(
        "--parquet-dir",
        "--sample-dir",
        dest="parquet_dir",
        default=str(DEFAULT_PARQUET_DIR),
        help=f"cache local dos parquet convertidos (padrao: {DEFAULT_PARQUET_DIR})",
    )
    parser.add_argument(
        "--prune-parquet",
        action="store_true",
        help="apaga do cache local os parquet que o manifesto ja confirma publicados",
    )
    parser.add_argument(
        "--reconciliar",
        action="store_true",
        help="adota no manifesto os arquivos ja publicados que nao constam nele, sem reenviar",
    )
    parser.add_argument("--force", action="store_true", help="reenvia mesmo com o checksum inalterado")
    parser.add_argument("--force-refresh", action="store_true", help="ignora o cache do catalogo de periodos")
    parser.add_argument("--limite", type=int, help="para depois de N arquivos (util para uma primeira carga)")
    parser.add_argument(
        "--commit-size",
        type=int,
        default=DEFAULT_COMMIT_SIZE,
        help=f"quantos arquivos agrupar por commit (padrao: {DEFAULT_COMMIT_SIZE})",
    )
    parser.add_argument("--cache-dir", help="diretorio de cache dos downloads")
    parser.add_argument("--create-repo", action="store_true", help="cria o repositorio no Hub se nao existir")
    parser.add_argument(
        "--update-card",
        action="store_true",
        help="reescreve o README.md do dataset no Hub (por padrao ele so e criado se nao existir)",
    )
    parser.add_argument(
        "--no-hub",
        action="store_true",
        help="dry run sem consultar o Hub: trata todo periodo como novo (incompativel com --push)",
    )
    parser.add_argument(
        "--log-dir", default=str(DEFAULT_LOG_DIR), help=f"onde gravar o log (padrao: {DEFAULT_LOG_DIR})"
    )
    parser.add_argument("--log-file", help="caminho exato do arquivo de log (sobrepoe --log-dir)")
    parser.add_argument("--no-log-file", action="store_true", help="nao grava log em arquivo")
    parser.add_argument("-v", "--verbose", action="store_true", help="mostra no console tambem o nivel DEBUG")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
