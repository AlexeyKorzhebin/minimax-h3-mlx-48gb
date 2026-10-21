"""Where the studio keeps its state. `H3_OUTDIR` must never point inside `~/models` (CLAUDE.md)."""
from __future__ import annotations

from pathlib import Path


class ConfigError(ValueError):
    pass


def resolve_outdir(raw: str | None, *, home: Path | None = None) -> Path:
    if not raw:
        raise ConfigError("нужен --outdir или переменная H3_OUTDIR")
    path = Path(raw).expanduser().resolve()
    models = ((home or Path.home()) / "models").resolve()
    if path == models or models in path.parents:
        raise ConfigError(f"H3_OUTDIR не может лежать внутри {models}: {path}")
    return path


def studio_dir(outdir: Path) -> Path:
    """`<outdir>/studio/` — the database, its backups, uploaded assets, providers.json/.env."""
    return outdir / "studio"
