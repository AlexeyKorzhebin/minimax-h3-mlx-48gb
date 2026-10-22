"""Where the studio keeps its state. `H3_OUTDIR` must never point inside `~/models` (CLAUDE.md)."""
from __future__ import annotations

import os
from pathlib import Path


class ConfigError(ValueError):
    pass


def resolve_outdir(raw: str | None, *, home: Path | None = None) -> Path:
    if not raw:
        raise ConfigError("нужен --outdir или переменная H3_OUTDIR")
    path = Path(raw).expanduser().resolve()
    models = ((home or Path.home()) / "models").resolve()
    if path == models or models in path.parents or _same_as_models(path, models):
        raise ConfigError(f"H3_OUTDIR не может лежать внутри {models}: {path}")
    return path


def _same_as_models(path: Path, models: Path) -> bool:
    """macOS volumes are case-insensitive: `~/MODELS/out` is `~/models/out`. Compare the path and
    each of its ancestors with `models` by identity (`samefile`), not by spelling."""
    if not models.exists():
        return False
    for candidate in (path, *path.parents):
        try:
            if candidate.exists() and os.path.samefile(candidate, models):
                return True
        except OSError:
            continue
    return False


def studio_dir(outdir: Path) -> Path:
    """`<outdir>/studio/` — the database, its backups, uploaded assets, providers.json/.env."""
    return outdir / "studio"
