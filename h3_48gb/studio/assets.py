"""Uploaded files (spec §7 O6, И7): stored once under `<outdir>/studio/assets/<id>.<ext>`, never
overwritten; the row is immutable (trigger in migration 001)."""
from __future__ import annotations

import hashlib
import io
import sqlite3
from pathlib import Path

from PIL import Image

from h3_48gb.studio import db, events, ids
from h3_48gb.studio.errors import StudioError

MAX_ASSET_BYTES = 25 * 1024 * 1024

#: Slice S1 takes pictures only (the hero's photo); voice, video and tracks arrive later (§2).
_PIL_FORMATS = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}


def sniff(data: bytes) -> tuple[str, str, str] | None:
    """(family, ext, mime) by leading bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "jpg", "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "webp", "image/webp"
    return None


def check_decodes(data: bytes, ext: str) -> None:
    """A right signature on broken content is still a broken picture: decode it fully."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            decoded_as = image.format
    except Exception:                       # Pillow raises half a dozen types for bad input
        raise StudioError(400, "asset_corrupt", "Картинка повреждена или не читается") from None
    if decoded_as != _PIL_FORMATS[ext]:
        raise StudioError(400, "asset_corrupt", "Картинка повреждена или не читается",
                          {"decoded_as": decoded_as})


def asset_out(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "kind": row["kind"], "sha256": row["sha256"], "bytes": row["bytes"],
            "mime": row["mime"], "url": f"/api/studio/assets/{row['id']}/file"}


def import_asset(conn: sqlite3.Connection, home: Path, *, data: bytes,
                 kind: str | None = None) -> dict:
    if len(data) > MAX_ASSET_BYTES:
        raise StudioError(413, "asset_too_large", "Файл больше 25 МБ",
                          {"bytes": len(data), "max": MAX_ASSET_BYTES})
    sniffed = sniff(data)
    if sniffed is None:
        raise StudioError(400, "asset_type_invalid", "Нужна картинка PNG, JPEG или WebP")
    family, ext, mime = sniffed
    if kind is not None and kind != family:
        raise StudioError(400, "asset_type_invalid", f"Файл — {family}, а ожидался {kind}",
                          {"sniffed": family, "kind": kind})
    check_decodes(data, ext)
    asset_id = ids.new_id("a")
    folder = home / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{asset_id}.{ext}"
    with open(path, "xb") as fh:          # "x": never overwrite an existing asset file (И7)
        fh.write(data)
    try:
        with db.tx(conn):
            conn.execute(
                "INSERT INTO asset (id, kind, path, sha256, bytes, mime, origin_kind, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'upload', ?)",
                (asset_id, family, str(path), hashlib.sha256(data).hexdigest(), len(data), mime,
                 ids.now_iso()))
            events.emit(conn, "asset_imported", payload={"asset_id": asset_id, "kind": family})
            return asset_out(conn.execute("SELECT * FROM asset WHERE id = ?",
                                          (asset_id,)).fetchone())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def asset_file(conn: sqlite3.Connection, asset_id: str) -> tuple[Path, str]:
    row = conn.execute("SELECT path, mime FROM asset WHERE id = ?", (asset_id,)).fetchone()
    if row is None or not Path(row["path"]).is_file():
        raise StudioError(404, "asset_not_found", "Файл не найден", {"asset_id": asset_id})
    return Path(row["path"]), row["mime"]
