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
    uv run --group publish python scripts/publish_to_hf.py --push     # for real

Pushing needs a write token in the ``HF_TOKEN`` environment variable;
``huggingface_hub`` picks it up on its own.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from brinss.datasets import _cache, _catalog, _reading
from brinss.datasets._families import FAMILIES
from brinss.datasets.enums import ColumnDtype, XlsxEngine

DEFAULT_REPO_ID = "agaqueiroz/brinss-public-datasets"
MANIFEST_PATH = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1
PERIOD_COLUMN = "periodo_referencia"

COMPRESSION = "zstd"

# Bumped whenever the conversion itself changes in a way that makes already
# published files stale (column typing, compression, the period column's
# representation). A mismatch forces a rewrite even when the source is
# untouched, which is the only way a recipe change ever reaches the Hub.
CONVERSION_RECIPE = f"v1|dtype={ColumnDtype.STRING.value}|compression={COMPRESSION}|period=str"

_HASH_CHUNK_BYTES = 1024 * 1024


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
    ]
    return "\n".join(lines)


def to_publishable(frame: pd.DataFrame) -> pd.DataFrame:
    """Make a frame safe to read outside pandas.

    ``periodo_referencia`` arrives as a ``pandas.Period``, which Parquet stores
    as a pandas-specific extension type over a raw month ordinal (2024-06 is
    written as 653). Pandas round-trips it, but the Hub's viewer, DuckDB and
    polars all see a meaningless integer -- so it goes out as "YYYY-MM" text.
    """
    if PERIOD_COLUMN in frame.columns:
        frame = frame.copy()
        frame[PERIOD_COLUMN] = frame[PERIOD_COLUMN].astype(str)
    return frame


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


# --------------------------------------------------------------------------
# planning and execution
# --------------------------------------------------------------------------


@dataclass
class Plan:
    uploads: list[tuple[str, str, str]] = field(default_factory=list)  # family, period, reason
    skipped: list[tuple[str, str]] = field(default_factory=list)  # family, period
    written_bytes: int = 0


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
    """
    path = cache_root / "files" / family_key / _cache._resource_filename(entry)
    return path if path.exists() else None


def _write_parquet(frame: pd.DataFrame, destination: Path) -> None:
    to_publishable(frame).to_parquet(destination, engine="pyarrow", compression=COMPRESSION, index=False)


def run(args: argparse.Namespace) -> int:
    cache_root = _cache.get_cache_root(args.cache_dir)
    family_keys = args.familia or list(FAMILIES)

    api = None
    manifest = empty_manifest()
    if args.push or not args.no_hub:
        from huggingface_hub import HfApi

        api = HfApi()
        if args.push and not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")):
            print("erro: --push exige um token de escrita em HF_TOKEN.", file=sys.stderr)
            return 2
        if args.push and args.create_repo:
            api.create_repo(args.repo, repo_type="dataset", exist_ok=True)
        manifest = _load_manifest(api, args.repo)

    plan = Plan()
    uploaded_any = False

    try:
        for family_key in family_keys:
            family = FAMILIES[family_key]
            catalog = _catalog.build_catalog(family, cache_dir=cache_root, force_refresh=args.force_refresh)
            entries = [e for e in catalog.entries if not args.periodo or str(e.period) in args.periodo]

            for entry in entries:
                if args.limite and len(plan.uploads) >= args.limite:
                    print(f"limite de {args.limite} arquivos atingido; parando.")
                    return _finish(api, args, manifest, plan, uploaded_any)

                period = str(entry.period)
                target = path_in_repo(family_key, period)

                source = _cached_source(entry, family_key, cache_root)
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
                with tempfile.TemporaryDirectory() as tmp:
                    local = Path(tmp) / f"{period}.parquet"
                    _write_parquet(frame, local)
                    size = local.stat().st_size
                    plan.written_bytes += size
                    print(f"  push  {target}  ({len(frame):,} linhas, {format_bytes(size)}, {reason})")
                    api.upload_file(
                        path_or_fileobj=str(local),
                        path_in_repo=target,
                        repo_id=args.repo,
                        repo_type="dataset",
                        commit_message=f"{family_key} {period} ({reason})",
                    )

                manifest["entries"][manifest_key(family_key, period)] = {
                    "source_sha256": digest,
                    "source_url": entry.url,
                    "resource_id": entry.resource_id,
                    "rows": len(frame),
                    "columns": len(frame.columns),
                    "parquet_bytes": size,
                    "conversion": CONVERSION_RECIPE,
                }
                uploaded_any = True
                del frame
    except KeyboardInterrupt:
        print("\ninterrompido; salvando o manifesto do que ja subiu.", file=sys.stderr)

    return _finish(api, args, manifest, plan, uploaded_any)


def _finish(api, args: argparse.Namespace, manifest: dict, plan: Plan, uploaded_any: bool) -> int:
    """Persist the manifest and the card, then summarize.

    The manifest goes up *after* the data files: if the run dies in between,
    the missing entries just make the next run redo those months. The opposite
    order would mark files as published that never made it.
    """
    if args.push and uploaded_any:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_file = Path(tmp) / MANIFEST_PATH
            manifest_file.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            api.upload_file(
                path_or_fileobj=str(manifest_file),
                path_in_repo=MANIFEST_PATH,
                repo_id=args.repo,
                repo_type="dataset",
                commit_message="Atualizar manifesto",
            )
            card = Path(tmp) / "README.md"
            card.write_text(build_dataset_card(sorted(FAMILIES)), encoding="utf-8")
            api.upload_file(
                path_or_fileobj=str(card),
                path_in_repo="README.md",
                repo_id=args.repo,
                repo_type="dataset",
                commit_message="Atualizar dataset card",
            )

    verb = "enviados" if args.push else "a enviar"
    print(
        f"\n{len(plan.uploads)} {verb}, {len(plan.skipped)} inalterados"
        + (f", {format_bytes(plan.written_bytes)} escritos" if plan.written_bytes else "")
    )
    if not args.push and plan.uploads:
        print("nada foi enviado (dry run). repita com --push para publicar.")
    return 0


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
    parser.add_argument("--force", action="store_true", help="reenvia mesmo com o checksum inalterado")
    parser.add_argument("--force-refresh", action="store_true", help="ignora o cache do catalogo de periodos")
    parser.add_argument("--limite", type=int, help="para depois de N arquivos (util para uma primeira carga)")
    parser.add_argument("--cache-dir", help="diretorio de cache dos downloads")
    parser.add_argument("--create-repo", action="store_true", help="cria o repositorio no Hub se nao existir")
    parser.add_argument(
        "--no-hub",
        action="store_true",
        help="nao consulta o Hub para montar o plano: trata todo periodo como novo",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
