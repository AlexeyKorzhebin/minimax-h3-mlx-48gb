"""Explicit studio operations (spec §7). The master and MCP both call these and nothing else.
Every write is one `db.tx` that also writes its event (И8)."""
from __future__ import annotations

import secrets
import sqlite3

from h3_48gb.studio import db, duration, events, ids, lint
from h3_48gb.studio.director import cast as cast_card
from h3_48gb.studio.errors import StudioError
from h3_48gb.studio.llm import Llm

FRAME_FORMATS = ("16:9", "9:16")


def _project_row(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise StudioError(404, "project_not_found", "Проект не найден",
                          {"project_id": project_id})
    return row


def require_project(conn: sqlite3.Connection, project_id: str) -> None:
    _project_row(conn, project_id)


def project_out(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "revision": row["revision"], "title": row["title"],
            "frame_format": row["frame_format"], "idea_text": row["idea_text"],
            "style_block": row["style_block"], "created_at": row["created_at"]}


def _check_frame_format(frame_format: str) -> None:
    if frame_format not in FRAME_FORMATS:
        raise StudioError(400, "args_invalid", "Формат кадра: 16:9 или 9:16",
                          {"frame_format": frame_format, "allowed": list(FRAME_FORMATS)})


def create_project(conn: sqlite3.Connection, *, title: str, frame_format: str,
                   idea_text: str = "") -> dict:
    _check_frame_format(frame_format)
    project_id = ids.new_id("p")
    with db.tx(conn):
        conn.execute(
            "INSERT INTO project (id, revision, title, frame_format, idea_text, created_at) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (project_id, title, frame_format, idea_text, ids.now_iso()))
        events.emit(conn, "project_created", project_id=project_id,
                    payload={"title": title, "frame_format": frame_format})
        return project_out(_project_row(conn, project_id))


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*, "
        " (SELECT COUNT(*) FROM scene s WHERE s.project_id = p.id AND s.deleted_at IS NULL)"
        "   AS scenes_total, "
        " (SELECT COUNT(*) FROM scene s WHERE s.project_id = p.id AND s.deleted_at IS NULL"
        "   AND s.approved_final_take_id IS NOT NULL) AS scenes_done "
        "FROM project p ORDER BY p.created_at DESC, p.id").fetchall()
    return [{**project_out(r), "scenes_total": r["scenes_total"], "scenes_done": r["scenes_done"]}
            for r in rows]


def _check_revision(row: sqlite3.Row, expected: int, current: dict) -> None:
    if row["revision"] != expected:
        raise StudioError(409, "revision_conflict",
                          "Данные изменились в другом окне — обновите и повторите",
                          {"current": current})


def update_project(conn: sqlite3.Connection, project_id: str, *, expected_revision: int,
                   title: str | None = None, frame_format: str | None = None) -> dict:
    if frame_format is not None:
        _check_frame_format(frame_format)
    with db.tx(conn):
        row = _project_row(conn, project_id)
        _check_revision(row, expected_revision, project_out(row))
        changes: dict = {}
        if title is not None and title != row["title"]:
            changes["title"] = title
        if frame_format is not None and frame_format != row["frame_format"]:
            done = conn.execute(
                "SELECT COUNT(*) FROM take t JOIN scene s ON s.id = t.scene_id "
                "WHERE s.project_id = ? AND t.status = 'done'", (project_id,)).fetchone()[0]
            if done:
                raise StudioError(409, "format_locked",
                                  "Формат кадра нельзя менять после первого готового дубля",
                                  {"done_takes": done})
            changes["frame_format"] = frame_format
        if not changes:
            return project_out(row)
        sets = ", ".join(f"{name} = ?" for name in changes)
        conn.execute(f"UPDATE project SET {sets}, revision = revision + 1 WHERE id = ?",
                     (*changes.values(), project_id))
        events.emit(conn, "project_updated", project_id=project_id, payload=changes)
        return project_out(_project_row(conn, project_id))


def snapshot(conn: sqlite3.Connection, project_id: str) -> dict:
    """O3: the project with everything the master draws, and the `last_seq` it is consistent
    with (И10) — read in one snapshot so an event committed in between cannot be lost."""
    with db.read(conn):
        project = project_out(_project_row(conn, project_id))
        return {"project": project,
                "scenes": [scene_out(conn, r) for r in _live_scenes(conn, project_id)],
                "cast": [cast_out(conn, r) for r in _cast_rows(conn, project_id)],
                "takes": [], "tasks": [], "last_seq": events.last_seq(conn)}


# --- slice S1: cast (task 9) ------------------------------------------------------------------


def _cast_rows(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM cast_member WHERE project_id = ? ORDER BY created_at, id",
                        (project_id,)).fetchall()


def cast_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    assets = conn.execute("SELECT asset_id, role, ord FROM cast_asset WHERE cast_id = ? "
                          "ORDER BY ord", (row["id"],)).fetchall()
    return {"id": row["id"], "tag": row["tag"], "description_ru": row["description_ru"],
            "description_en": row["description_en"], "portrait_phrase": row["portrait_phrase"],
            "assets": [{"asset_id": a["asset_id"], "role": a["role"], "ord": a["ord"],
                        "url": f"/api/studio/assets/{a['asset_id']}/file"} for a in assets]}


def add_cast(conn: sqlite3.Connection, project_id: str, *, expected_revision: int, tag: str,
             description_ru: str, assets: list[dict], description_en: str | None,
             portrait_phrase: str | None, llm: Llm) -> dict:
    """O5. The English card comes from the owner (both fields) or from the LLM — outside the
    write transaction, so a slow model never holds the database."""
    row = _project_row(conn, project_id)
    _check_revision(row, expected_revision, project_out(row))
    if (description_en is None) != (portrait_phrase is None):
        missing = "description_en" if description_en is None else "portrait_phrase"
        raise StudioError(400, "cast_card_invalid",
                          f"Карточка вручную — оба поля или ни одного: нет {missing}",
                          {"field": missing})
    if description_en is not None and portrait_phrase is not None:
        card = {"description_en": description_en.strip(),
                "portrait_phrase": portrait_phrase.strip().rstrip(". ")}
        cast_card.check_card(card["description_en"], card["portrait_phrase"])
    else:
        card = cast_card.describe(llm, tag=tag, description_ru=description_ru)
    cast_id = ids.new_id("c")
    with db.tx(conn):
        row = _project_row(conn, project_id)
        _check_revision(row, expected_revision, project_out(row))
        if conn.execute("SELECT 1 FROM cast_member WHERE project_id = ? AND tag = ?",
                        (project_id, tag)).fetchone():
            raise StudioError(409, "cast_tag_taken", f"@{tag} уже есть в кастинге", {"tag": tag})
        conn.execute(
            "INSERT INTO cast_member (id, project_id, tag, description_ru, description_en, "
            "portrait_phrase, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cast_id, project_id, tag, description_ru, card["description_en"],
             card["portrait_phrase"], ids.now_iso()))
        for ord_, item in enumerate(assets, start=1):
            asset = conn.execute("SELECT kind FROM asset WHERE id = ?",
                                 (item["asset_id"],)).fetchone()
            if asset is None or asset["kind"] != "image":
                raise StudioError(400, "asset_unknown",
                                  f"Файл {item['asset_id']} не подходит для роли {item['role']}",
                                  {"asset_id": item["asset_id"], "role": item["role"]})
            conn.execute("INSERT INTO cast_asset (cast_id, asset_id, role, ord) "
                         "VALUES (?, ?, ?, ?)", (cast_id, item["asset_id"], item["role"], ord_))
        conn.execute("UPDATE project SET revision = revision + 1 WHERE id = ?", (project_id,))
        events.emit(conn, "cast_added", project_id=project_id, payload={"tag": tag})
        member = cast_out(conn, conn.execute("SELECT * FROM cast_member WHERE id = ?",
                                             (cast_id,)).fetchone())
        return {"cast_member": member, "project": project_out(_project_row(conn, project_id))}


# --- slice S1: scenes and lint (task 10) ------------------------------------------------------

TEXT_FIELDS = lint.TEXT_FIELDS
#: И1: a change to any of these (or to the scene's refs) is a new content revision.
FINGERPRINT_FIELDS = ("mode", "action_text", "performance_text", "sound_text", "music_text",
                      "frames", "seed", "source_frame_kind", "source_scene_id",
                      "source_frame_asset_id")
STORED_FIELDS = ("mode", *TEXT_FIELDS, "requested_s", "frames", "seed", "source_frame_kind",
                 "source_scene_id", "source_frame_asset_id")


def _new_seed() -> int:
    return secrets.randbelow(2**31 - 1) + 1


def _refs(conn: sqlite3.Connection, scene_id: str) -> list[tuple[str, int, str]]:
    return [tuple(r) for r in conn.execute(
        "SELECT cast_id, ord, role FROM scene_ref WHERE scene_id = ? ORDER BY ord", (scene_id,))]


def _ref_tags(conn: sqlite3.Connection, scene_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT c.tag FROM scene_ref r JOIN cast_member c ON c.id = r.cast_id "
        "WHERE r.scene_id = ? ORDER BY r.ord", (scene_id,))]


def scene_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {"id": row["id"], "idx": row["idx"], "revision": row["revision"],
            "state_version": row["state_version"], "edit_version": row["edit_version"],
            "mode": row["mode"],
            **{f: row[f] for f in TEXT_FIELDS},
            "requested_s": row["requested_s"], "frames": row["frames"],
            "duration_s": round(duration.seconds(row["frames"]), 3), "seed": row["seed"],
            "source_frame": {"kind": row["source_frame_kind"], "scene_id": row["source_scene_id"],
                             "asset_id": row["source_frame_asset_id"]},
            "refs": _ref_tags(conn, row["id"]), "review_state": row["review_state"],
            "approved_sketch_take_id": row["approved_sketch_take_id"],
            "approved_final_take_id": row["approved_final_take_id"]}


def _live_scenes(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM scene WHERE project_id = ? AND deleted_at IS NULL "
                        "ORDER BY idx", (project_id,)).fetchall()


def _scene_row(conn: sqlite3.Connection, scene_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM scene WHERE id = ? AND deleted_at IS NULL",
                       (scene_id,)).fetchone()
    if row is None:
        raise StudioError(404, "scene_not_found", "Сцена не найдена", {"scene_id": scene_id})
    return row


def _fingerprint(fields, refs: list[tuple[str, int, str]]) -> tuple:
    return (*(fields[name] for name in FINGERPRINT_FIELDS), tuple(refs))


def _resolve_source(conn: sqlite3.Connection, source: dict, *, prev_id: str | None,
                    allowed_scene_ids: set[str]) -> tuple[str, str | None, str | None]:
    kind = source["kind"]
    if kind == "none":
        return "none", None, None
    if kind == "prev_last_frame":
        scene_id = source.get("scene_id") or prev_id
        if scene_id is not None and scene_id not in allowed_scene_ids:
            raise StudioError(400, "source_scene_unknown", "Источник кадра — не сцена этого проекта",
                              {"scene_id": scene_id})
        return "prev_last_frame", scene_id, None
    asset_id = source.get("asset_id")
    row = conn.execute("SELECT kind FROM asset WHERE id = ?", (asset_id,)).fetchone()
    if row is None or row["kind"] not in ("image", "frame"):
        raise StudioError(400, "asset_unknown", "Кадр для сцены не найден",
                          {"asset_id": asset_id})
    return "asset", None, asset_id


def _refs_from_tags(cast_by_tag: dict, tags: list[str]) -> list[tuple[str, int, str]]:
    if len(tags) != len(set(tags)):
        raise StudioError(400, "args_invalid", "Тег повторяется в референсах сцены",
                          {"refs": tags})
    out = []
    for ord_, tag in enumerate(tags, start=1):
        member = cast_by_tag.get(tag)
        if member is None:
            raise StudioError(400, "cast_unknown", f"В кастинге нет @{tag}", {"tag": tag})
        out.append((member["id"], ord_, "person"))
    return out


def _write_refs(conn: sqlite3.Connection, scene_id: str, refs: list[tuple[str, int, str]]):
    conn.execute("DELETE FROM scene_ref WHERE scene_id = ?", (scene_id,))
    conn.executemany("INSERT INTO scene_ref (scene_id, cast_id, ord, role) VALUES (?, ?, ?, ?)",
                     [(scene_id, *r) for r in refs])


def lint_project(conn: sqlite3.Connection, project_id: str,
                 scene_ids: list[str] | None = None) -> dict[str, list[dict]]:
    """O9. Findings for every live scene (or the listed ones), keyed by scene id."""
    rows = _live_scenes(conn, project_id)
    project = lint.LintProject(
        scenes=[lint.LintScene(**{k: r[k] for k in (
            "id", "idx", "mode", *TEXT_FIELDS, "requested_s", "frames", "source_frame_kind",
            "source_scene_id")}, cast_tags=tuple(_ref_tags(conn, r["id"]))) for r in rows],
        portrait_phrases={c["tag"]: c["portrait_phrase"] for c in _cast_rows(conn, project_id)})
    wanted = set(scene_ids) if scene_ids is not None else {r["id"] for r in rows}
    unknown = wanted - {r["id"] for r in rows}
    if unknown:
        raise StudioError(404, "scene_not_found", "Сцена не найдена",
                          {"scene_ids": sorted(unknown)})
    return {s.id: lint.lint_scene(s, project) for s in project.scenes if s.id in wanted}


def update_scene(conn: sqlite3.Connection, scene_id: str, *, expected_edit_version: int,
                 changes: dict) -> dict:
    """O8. The edit conflict is decided by `edit_version` (И1a: +1 on ANY change of an editable
    field), not by `revision`: И1 leaves `revision` alone when only `requested_s` (same frames)
    or `state_in_text` changes, and a stale tab would then overwrite another tab's saved value.
    `revision` keeps its И1 meaning (fingerprint for takes). The project revision is not touched,
    so editing one scene never stales another's plan (§7)."""
    with db.tx(conn):
        row = _scene_row(conn, scene_id)
        if row["edit_version"] != expected_edit_version:
            raise StudioError(409, "revision_conflict",
                              "Сцена изменилась в другом окне — обновите и повторите",
                              {"current": scene_out(conn, row)})
        fields = {name: row[name] for name in STORED_FIELDS}
        for name in ("mode", *TEXT_FIELDS, "seed"):
            if changes.get(name) is not None:
                fields[name] = changes[name]
        if changes.get("requested_s") is not None:
            fields["requested_s"] = changes["requested_s"]
            fields["frames"] = duration.snap_frames(changes["requested_s"])
        live = _live_scenes(conn, row["project_id"])
        if changes.get("source_frame") is not None:
            earlier = [r["id"] for r in live if r["idx"] < row["idx"]]
            kind, src_scene, src_asset = _resolve_source(
                conn, changes["source_frame"], prev_id=earlier[-1] if earlier else None,
                allowed_scene_ids={r["id"] for r in live} - {scene_id})
            fields.update(source_frame_kind=kind, source_scene_id=src_scene,
                          source_frame_asset_id=src_asset)
        old_refs = _refs(conn, scene_id)
        refs = old_refs
        if changes.get("refs") is not None:
            cast_by_tag = {c["tag"]: c for c in _cast_rows(conn, row["project_id"])}
            refs = _refs_from_tags(cast_by_tag, changes["refs"])
        changed = sorted(n for n in STORED_FIELDS if fields[n] != row[n])
        if refs != old_refs:
            changed.append("refs")
        if changed:
            bump = 0 if _fingerprint(fields, refs) == _fingerprint(row, old_refs) else 1
            edit_bump = 1 if changed else 0          # И1a: every editable field counts
            sets = ", ".join(f"{name} = ?" for name in STORED_FIELDS)
            conn.execute(f"UPDATE scene SET {sets}, revision = revision + ?, "
                         f"edit_version = edit_version + ? WHERE id = ?",
                         (*(fields[n] for n in STORED_FIELDS), bump, edit_bump, scene_id))
            if refs != old_refs:
                _write_refs(conn, scene_id, refs)
            events.emit(conn, "scene_updated", project_id=row["project_id"], scene_id=scene_id,
                        payload={"revision": row["revision"] + bump,
                                 "edit_version": row["edit_version"] + edit_bump,
                                 "fields": changed})
        out = scene_out(conn, _scene_row(conn, scene_id))
    findings = lint_project(conn, row["project_id"], [scene_id])[scene_id]
    # `cascade` (И11) is the list of scenes whose approval this edit revoked; approvals exist
    # from slice S3 on, so in S1 it is always empty.
    return {"scene": out, "cascade": [], "lint": findings}


def replace_scenes(conn: sqlite3.Connection, project_id: str, *, expected_revision: int,
                   scenes: list[dict], deleted: list[dict] | None = None,
                   event_kind: str = "scenes_replaced",
                   event_payload: dict | None = None,
                   in_tx_hook=None) -> dict:
    """O7 (and O26 through it). Identity is `scene_id`; array order is `idx`; a scene missing
    from the array is soft-deleted; a matched scene keeps its revision unless its fingerprint
    changed (И1). A `prev_last_frame` source without an explicit scene is the previous scene of
    the array, so a reorder that changes it is a content change (§7 «Сопоставление сцен»).
    Two locks: `expected_revision` of the project guards the set (add, delete, reorder —
    O4/O5/O7/O26 bump it), and every existing scene carries its own `expected_edit_version`
    (И1a) — an O8 in another tab does not bump the project, so without it O7 would write the
    tab's old copy of that scene over the saved edit. A deletion is an edit too (review r7):
    every live scene must be either kept in `scenes` or listed in `deleted` with the version
    the tab read; a scene in neither, or deleted at a stale version, is a 409."""
    with db.tx(conn):
        prow = _project_row(conn, project_id)
        _check_revision(prow, expected_revision, project_out(prow))
        live = {r["id"]: r for r in _live_scenes(conn, project_id)}
        given = [s.get("scene_id") for s in scenes]
        known = [sid for sid in given if sid is not None]
        if len(known) != len(set(known)):
            raise StudioError(400, "args_invalid", "scene_id повторяется", {"scene_ids": known})
        for sid in known:
            if sid not in live:
                raise StudioError(400, "scene_unknown", "Такой сцены в проекте нет",
                                  {"scene_id": sid})
        removing = {d["scene_id"]: d["expected_edit_version"] for d in (deleted or [])}
        for sid, expected in removing.items():
            if sid not in live:
                raise StudioError(400, "scene_unknown", "Такой сцены в проекте нет",
                                  {"scene_id": sid})
            if sid in known:
                raise StudioError(400, "args_invalid", "Сцена и оставлена, и удалена",
                                  {"scene_id": sid})
            if expected != live[sid]["edit_version"]:
                raise StudioError(409, "revision_conflict",
                                  "Удаляемую сцену изменили в другом окне — обновите и решите снова",
                                  {"scene_id": sid, "current": scene_out(conn, live[sid])})
        for sid in live:
            if sid not in known and sid not in removing:
                raise StudioError(409, "revision_conflict",
                                  "В проекте есть сцена, которой нет ни среди оставленных, ни среди "
                                  "удаляемых — обновите", {"scene_id": sid,
                                                          "current": scene_out(conn, live[sid])})
        for item in scenes:
            sid = item.get("scene_id")
            if sid is None:
                continue
            if item.get("expected_edit_version") is None:
                raise StudioError(400, "args_invalid", "Для существующей сцены нужен "
                                  "expected_edit_version", {"scene_id": sid})
            if item["expected_edit_version"] != live[sid]["edit_version"]:
                raise StudioError(409, "revision_conflict",
                                  "Сцена изменилась в другом окне — обновите и повторите",
                                  {"scene_id": sid, "current": scene_out(conn, live[sid])})
        final_ids = [sid or ids.new_id("s") for sid in given]
        cast_by_tag = {c["tag"]: c for c in _cast_rows(conn, project_id)}
        diff: dict[str, list[str]] = {"added": [], "unchanged": [], "changed": [], "moved": [],
                                      "deleted": []}
        now = ids.now_iso()
        # Free the live (project_id, idx) slots before writing the new order.
        conn.execute("UPDATE scene SET idx = -1 - idx WHERE project_id = ? AND deleted_at IS NULL",
                     (project_id,))
        for idx, (sid, item) in enumerate(zip(final_ids, scenes)):
            kind, src_scene, src_asset = _resolve_source(
                conn, item["source_frame"], prev_id=final_ids[idx - 1] if idx else None,
                allowed_scene_ids=set(final_ids) - {sid})
            refs = _refs_from_tags(cast_by_tag, item["refs"])
            old = live.get(sid)
            fields = {"mode": item["mode"], **{f: item[f] for f in TEXT_FIELDS},
                      "requested_s": item["requested_s"],
                      "frames": duration.snap_frames(item["requested_s"]),
                      "seed": item["seed"] or (old["seed"] if old else _new_seed()),
                      "source_frame_kind": kind, "source_scene_id": src_scene,
                      "source_frame_asset_id": src_asset}
            names = ", ".join(STORED_FIELDS)
            marks = ", ".join("?" for _ in STORED_FIELDS)
            if old is None:
                conn.execute(f"INSERT INTO scene (id, project_id, idx, created_at, {names}) "
                             f"VALUES (?, ?, ?, ?, {marks})",
                             (sid, project_id, idx, now, *(fields[n] for n in STORED_FIELDS)))
                _write_refs(conn, sid, refs)
                diff["added"].append(sid)
                continue
            old_refs = _refs(conn, sid)
            same = _fingerprint(fields, refs) == _fingerprint(old, old_refs)
            edited = refs != old_refs or any(fields[n] != old[n] for n in STORED_FIELDS)
            sets = ", ".join(f"{n} = ?" for n in STORED_FIELDS)
            conn.execute(f"UPDATE scene SET idx = ?, {sets}, revision = revision + ?, "
                         f"edit_version = edit_version + ? WHERE id = ?",
                         (idx, *(fields[n] for n in STORED_FIELDS), 0 if same else 1,
                          1 if edited else 0, sid))
            if refs != old_refs:
                _write_refs(conn, sid, refs)
            if not same:
                diff["changed"].append(sid)
            elif old["idx"] != idx:
                diff["moved"].append(sid)
            else:
                diff["unchanged"].append(sid)
        for sid in live:
            if sid not in final_ids:
                conn.execute("UPDATE scene SET deleted_at = ? WHERE id = ?", (now, sid))
                diff["deleted"].append(sid)
        conn.execute("UPDATE project SET revision = revision + 1 WHERE id = ?", (project_id,))
        if in_tx_hook is not None:
            in_tx_hook(conn)
        events.emit(conn, event_kind, project_id=project_id,
                    payload={"diff": diff, **(event_payload or {})})
        out = [scene_out(conn, r) for r in _live_scenes(conn, project_id)]
        project = project_out(_project_row(conn, project_id))
    return {"project": project, "scenes": out, "diff": diff,
            "lint": lint_project(conn, project_id)}
