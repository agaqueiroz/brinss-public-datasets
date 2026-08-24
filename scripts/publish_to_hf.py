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

Note the asymmetry this creates: the portal publishes no checksum, so deciding
whether a month changed still requires having the source file locally. The
manifest saves the expensive half (parsing, conversion, upload), not the
download -- and on a machine with a warm cache the download is a no-op anyway.

Usage::

    uv run --group publish python scripts/publish_to_hf.py            # dry run
    uv run --group publish python scripts/publish_to_hf.py --sample   # local only
    uv run --group publish python scripts/publish_to_hf.py --push     # for real

``--sample`` converts whatever is already in the download cache into ``tmp/``,
mirroring the layout the Hub would get. It never downloads and never contacts
the Hub, which makes it the cheap way to eyeball the real output: a full
``--push`` is 291 files and pulls tens of GB from the portal.

Pushing needs a write token, either in ``HF_TOKEN`` or stored on disk by
``hf auth login``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from brinss.datasets import _cache, _catalog, _reading
from brinss.datasets._families import FAMILIES
from brinss.datasets.enums import ColumnDtype, XlsxEngine

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLE_DIR = REPO_ROOT / "tmp"

DEFAULT_REPO_ID = "agaqueiroz/brinss-public-datasets"
MANIFEST_PATH = "manifest.json"
CARD_PATH = "README.md"
MANIFEST_SCHEMA_VERSION = 1
PERIOD_COLUMN = "periodo_referencia"

COMPRESSION = "zstd"

# Bumped whenever the conversion itself changes in a way that makes already
# published files stale (column typing, compression, the period column's
# representation). A mismatch forces a rewrite even when the source is
# untouched, which is the only way a recipe change ever reaches the Hub.
CONVERSION_RECIPE = f"v1|dtype={ColumnDtype.STRING.value}|compression={COMPRESSION}|period=str"

_HASH_CHUNK_BYTES = 1024 * 1024

# A batch is flushed once it reaches either bound. The file count keeps the
# commit log readable; the byte cap keeps the staged Parquet files from filling
# the disk, which matters on the families whose months run to gigabytes.
DEFAULT_COMMIT_SIZE = 25
COMMIT_BYTE_CAP = 2 * 1024**3


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
        "e da receita de conversão usada.",
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


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    raise AssertionError("unreachable: the GB branch always returns")


# --------------------------------------------------------------------------
# planning and execution
# --------------------------------------------------------------------------


@dataclass
class Plan:
    uploads: list[tuple[str, str, str]] = field(default_factory=list)  # family, period, reason
    skipped: list[tuple[str, str]] = field(default_factory=list)  # family, period
    written_bytes: int = 0


@dataclass
class CommitBatch:
    """Groups Parquet files into one Hub commit instead of one commit each.

    A file-per-commit first load is roughly 300 sequential commits: slow, hard
    on the Hub's rate limiting, and it buries the repository history.

    Manifest entries ride along and are merged only once their commit lands, so
    the invariant the manifest depends on is preserved -- data reaches the Hub
    before the manifest claims it did, never the other way round.
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
        targets = [op.path_in_repo for op in self.operations]
        message = f"Publicar {len(targets)} arquivo(s): {targets[0]}"
        if len(targets) > 1:
            message += f" ... {targets[-1]}"
        self.api.create_commit(
            repo_id=self.repo_id, repo_type="dataset", operations=self.operations, commit_message=message
        )
        self.commits += 1
        for operation in self.operations:
            Path(operation.path_or_fileobj).unlink(missing_ok=True)
        for key, value in self.pending:
            self.manifest["entries"][key] = value
        self.operations.clear()
        self.pending.clear()
        self.pending_bytes = 0


def _load_manifest(api, repo_id: str) -> dict:
    from huggingface_hub.errors import RepositoryNotFoundError

    try:
        if not api.file_exists(repo_id, MANIFEST_PATH, repo_type="dataset"):
            return empty_manifest()
        local = api.hf_hub_download(repo_id=repo_id, filename=MANIFEST_PATH, repo_type="dataset")
    except RepositoryNotFoundError:
        return empty_manifest()
    manifest = json.loads(Path(local).read_text(encoding="utf-8"))
    manifest.setdefault("entries", {})
    return manifest


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


def _sample_one(
    args: argparse.Namespace,
    entry,
    family_key: str,
    period: str,
    source: Path | None,
    target: str,
    plan: Plan,
) -> None:
    """Convert one cached month into the local sample tree.

    Deliberately never downloads: the point of --sample is to inspect the real
    output without paying for the catalogue. A month that is not cached is
    reported and passed over rather than fetched.
    """
    if source is None:
        plan.skipped.append((family_key, period))
        print(f"  skip  {family_key}/{period}  (nao esta em cache)")
        return

    # Mirrors path_in_repo so the local tree matches what the Hub would hold.
    local = Path(args.sample_dir) / target
    if local.exists() and not args.force:
        plan.skipped.append((family_key, period))
        print(f"  skip  {local}  (ja gerado; use --force para refazer)")
        return

    frame = _reading.read_resource(
        source, entry, columns=None, engine=XlsxEngine.OPENPYXL, dtype=ColumnDtype.STRING
    )
    local.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet(frame, local)
    size = local.stat().st_size
    plan.written_bytes += size
    plan.uploads.append((family_key, period, "amostra"))
    print(f"  gera  {local}  ({len(frame):,} linhas, {format_bytes(size)})")


def _write_parquet(frame: pd.DataFrame, destination: Path) -> None:
    to_publishable(frame).to_parquet(destination, engine="pyarrow", compression=COMPRESSION, index=False)


def run(args: argparse.Namespace) -> int:
    if args.no_hub and args.push:
        print("erro: --no-hub e --push sao incompativeis (publicar exige ler o manifesto).", file=sys.stderr)
        return 2
    if args.sample and args.push:
        print("erro: --sample e --push sao incompativeis (--sample so escreve localmente).", file=sys.stderr)
        return 2

    requested_periods = normalize_periodos(args.periodo)
    cache_root = _cache.get_cache_root(args.cache_dir)
    family_keys = args.familia or list(FAMILIES)

    api = None
    manifest = empty_manifest()
    if not args.no_hub and not args.sample:
        from huggingface_hub import HfApi, get_token

        # get_token() walks several sources: an OIDC exchange when
        # HF_OIDC_RESOURCE is set (Trusted Publishers in CI), then HF_TOKEN,
        # then HUGGING_FACE_HUB_TOKEN, then the token file written by
        # `hf auth login`, then Colab secrets. Reading the environment alone
        # would reject a perfectly good CLI session.
        if args.push and not get_token():
            print(
                "erro: --push exige um token de escrita. Defina HF_TOKEN ou rode `hf auth login`.",
                file=sys.stderr,
            )
            return 2
        api = HfApi()
        if args.push and args.create_repo:
            api.create_repo(args.repo, repo_type="dataset", exist_ok=True)
        manifest = _load_manifest(api, args.repo)

    plan = Plan()
    matched_periods: set[str] = set()
    batch = CommitBatch(api=api, repo_id=args.repo, manifest=manifest, max_files=args.commit_size)
    staging = tempfile.TemporaryDirectory()

    try:
        for family_key in family_keys:
            family = FAMILIES[family_key]
            catalog = _catalog.build_catalog(family, cache_dir=cache_root, force_refresh=args.force_refresh)

            for entry in catalog.entries:
                period = str(entry.period)
                if requested_periods is not None:
                    if period not in requested_periods:
                        continue
                    matched_periods.add(period)

                if args.limite and len(plan.uploads) >= args.limite:
                    print(f"limite de {args.limite} arquivos atingido; parando.")
                    return _finish(api, args, manifest, plan, batch, matched_periods, requested_periods, staging)

                target = path_in_repo(family_key, period)
                source = _cached_source(entry, family_key, cache_root)

                if args.sample:
                    _sample_one(args, entry, family_key, period, source, target, plan)
                    continue

                if source is None and not args.push:
                    # Cannot know whether it changed without the bytes, and a
                    # dry run is not worth a multi-GB download.
                    plan.uploads.append((family_key, period, "fonte nao baixada"))
                    print(f"  PLAN  {target}  (fonte ainda nao baixada)")
                    continue
                if source is None:
                    source = _cache.fetch_resource(entry, family_key=family_key, cache_dir=cache_root)
                digest = sha256_file(source)

                should, reason = needs_upload(manifest, family_key, period, digest)
                if args.force and not should:
                    should, reason = True, "forcado"
                if not should:
                    plan.skipped.append((family_key, period))
                    print(f"  skip  {family_key}/{period}  ({reason})")
                    continue

                plan.uploads.append((family_key, period, reason))
                if not args.push:
                    print(f"  PLAN  {target}  ({reason})")
                    continue

                frame = _reading.read_resource(
                    source, entry, columns=None, engine=XlsxEngine.OPENPYXL, dtype=ColumnDtype.STRING
                )
                local = Path(staging.name) / f"{family_key}__{period}.parquet"
                _write_parquet(frame, local)
                size = local.stat().st_size
                plan.written_bytes += size
                print(f"  push  {target}  ({len(frame):,} linhas, {format_bytes(size)}, {reason})")

                manifest_entry = {
                    "source_sha256": digest,
                    "source_url": entry.url,
                    "resource_id": entry.resource_id,
                    "rows": len(frame),
                    "columns": len(frame.columns),
                    "parquet_bytes": size,
                    "conversion": CONVERSION_RECIPE,
                }
                del frame
                batch.add(local, target, (manifest_key(family_key, period), manifest_entry), size)

            if args.push:
                # Keep every commit within one family, so the message reads well.
                batch.flush()
    except KeyboardInterrupt:
        print("\ninterrompido; publicando o que ja foi convertido.", file=sys.stderr)

    return _finish(api, args, manifest, plan, batch, matched_periods, requested_periods, staging)


def _finish(
    api,
    args: argparse.Namespace,
    manifest: dict,
    plan: Plan,
    batch: CommitBatch,
    matched_periods: set[str],
    requested_periods: list[str] | None,
    staging: tempfile.TemporaryDirectory,
) -> int:
    """Land the last batch, persist the manifest and card, then summarize.

    The manifest goes up *after* the data files: if the run dies in between,
    the missing entries just make the next run redo those months. The opposite
    order would mark files as published that never made it.
    """
    try:
        if args.push:
            batch.flush()
            if batch.commits:
                _upload_json(api, args.repo, MANIFEST_PATH, manifest, "Atualizar manifesto")
                _maybe_upload_card(api, args.repo, manifest, update=args.update_card)
    finally:
        staging.cleanup()

    if args.sample:
        summary = f"\n{len(plan.uploads)} gerados, {len(plan.skipped)} pulados"
        if plan.written_bytes:
            summary += f", {format_bytes(plan.written_bytes)} em {args.sample_dir}"
        print(summary)
    else:
        verb = "enviados" if args.push else "a enviar"
        summary = f"\n{len(plan.uploads)} {verb}, {len(plan.skipped)} inalterados"
        if plan.written_bytes:
            summary += f", {format_bytes(plan.written_bytes)} em {batch.commits} commit(s)"
        print(summary)
        if not args.push and plan.uploads:
            print("nada foi enviado (dry run). repita com --push para publicar.")

    unmatched = sorted(set(requested_periods or []) - matched_periods)
    if unmatched:
        print(
            f"erro: nenhum recurso encontrado para: {', '.join(unmatched)}.",
            file=sys.stderr,
        )
        return 1
    return 0


def _upload_json(api, repo_id: str, path: str, payload: dict, message: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / Path(path).name
        local.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        api.upload_file(
            path_or_fileobj=str(local),
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
        "--sample-dir",
        default=str(DEFAULT_SAMPLE_DIR),
        help=f"onde --sample grava os parquet (padrao: {DEFAULT_SAMPLE_DIR})",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
