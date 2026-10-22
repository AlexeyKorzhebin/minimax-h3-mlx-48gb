"""«Сценарист» (spec §9.1): idea -> scenes. The model writes the words; the code does the
arithmetic — number of scenes, lengths, modes and sources, the verbatim portrait phrase."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from pydantic import ValidationError

from h3_48gb.studio.errors import StudioError
from h3_48gb.studio import duration, schemas
from h3_48gb.studio.llm import Llm, validate_reply

PROMPT = Path(__file__).parent / "prompts" / "scenarist.md"
#: One LLM call writes at most this many scenes (its output budget, see the prompt). This is the
#: scenarist's limit, not the project's: a project has no upper bound on scenes (§4.4, owner 21.10).
MAX_SCENES_PER_PROPOSAL = 12

_STR = {"type": "string"}
STUDIO_SCENES_SCHEMA = {
    "name": "studio_scenes",
    "schema": {
        "type": "object",
        "properties": {
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": _STR, "performance": _STR, "sound": _STR, "music": _STR,
                        "state_in": _STR, "state_out": _STR,
                        "cast_tags": {"type": "array", "items": _STR},
                        # No bounds here: a model that answers 16 or 1 is clamped by the code (R6), not refused.
                        "requested_s": {"type": "number"},
                    },
                    "required": ["action", "performance", "sound", "music", "state_in",
                                 "state_out", "cast_tags", "requested_s"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["scenes"],
        "additionalProperties": False,
    },
}
RETRY_REMINDER = ("Ответ строго одним JSON-объектом {\"scenes\": [...]} по схеме, без другого "
                  "текста.")


def prompt_version() -> str:
    return hashlib.sha256(PROMPT.read_bytes()).hexdigest()[:12]


_TAG = re.compile(schemas.TAG_PATTERN)
MAX_DROPPED_TAGS = 12


def _move_to_front(text: str, phrase: str) -> str:
    i = text.index(phrase)
    before, after = text[:i].rstrip(), text[i + len(phrase):].lstrip(" .,;")
    if before and after:
        return before + ("" if before[-1] in ".!?" else ".") + " " + after
    return before or after


def _with_portraits(action: str, phrases: list[str]) -> str:
    """Each phrase verbatim at the start of the action, in cast order, exactly once: absent -> put
    in front; already at the start -> left alone; somewhere inside -> moved to the front."""
    for phrase in reversed([p for p in phrases if p]):
        if action.startswith(phrase):
            continue
        if phrase in action:
            action = _move_to_front(action, phrase)
        action = phrase + ". " + action if action else phrase + "."
    return action


def propose(llm: Llm, *, idea_text: str, scenes_count: int, frame_format: str,
            style_block: str, cast: list[dict]) -> tuple[list[dict], dict, list[str]]:
    """Return (scenes in the O7 input shape, llm_json for the proposal row, dropped tags).

    `cast` items: `{"tag", "description_en", "portrait_phrase", "has_image"}`. A scene's `refs`
    are the cast members on screen (SCENE_REF); pictures of them condition the render only in
    `refs` mode (S3)."""
    if scenes_count > MAX_SCENES_PER_PROPOSAL:
        raise StudioError(400, "too_many_scenes_for_scenarist",
                          f"Сценарист пишет не больше {MAX_SCENES_PER_PROPOSAL} сцен за раз; "
                          f"остальные добавьте «+ Сцена»",
                          {"max": MAX_SCENES_PER_PROPOSAL, "wanted": scenes_count})
    user = {"idea": idea_text, "scenes_count": scenes_count, "frame_format": frame_format,
            "style_block": style_block,
            "cast": [{k: c[k] for k in ("tag", "description_en", "portrait_phrase")}
                     for c in cast]}
    messages = [{"role": "system", "content": PROMPT.read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]
    raw = llm.json_turn(messages, STUDIO_SCENES_SCHEMA, RETRY_REMINDER)
    validate_reply(raw, STUDIO_SCENES_SCHEMA)
    got = raw["scenes"]
    if len(got) != scenes_count:
        raise StudioError(502, "llm_failed",
                          f"Сценарист вернул {len(got)} сцен вместо {scenes_count}",
                          {"got": len(got), "wanted": scenes_count})
    for i, s in enumerate(got):
        # json.loads accepts NaN and Infinity; they pass `type: number` and then poison the row.
        if not math.isfinite(s["requested_s"]):
            raise StudioError(502, "llm_failed", "Модель ответила не по схеме",
                              {"schema": STUDIO_SCENES_SCHEMA["name"],
                               "path": f"/scenes/{i}/requested_s", "rule": "finite"})
    by_tag = {c["tag"]: c for c in cast}
    scenes, dropped = [], []
    for i, s in enumerate(got):
        tags: list[str] = []
        for raw_tag in s["cast_tags"]:
            tag = raw_tag.lstrip("@").lower()          # cast tags are lowercase (schemas.Tag)
            if tag not in by_tag:
                if (_TAG.fullmatch(tag) and tag not in dropped
                        and len(dropped) < MAX_DROPPED_TAGS):
                    dropped.append(tag)
            elif tag not in tags:
                tags.append(tag)
        if i == 0 and not tags:
            # The opening scene with nobody listed: the model forgot the field (live probe
            # 23.10, 1 of 5). A pictured hero's face enters the chain only here — every later
            # scene is from_frame (§9.1) — and the owner cast them for this video (§4.4 шаг 2),
            # so the opening scene shows the pictured cast unless the model named someone else.
            tags = [c["tag"] for c in cast if c["has_image"]]
        with_image = [t for t in tags if by_tag[t]["has_image"]]
        if i == 0:
            mode = "refs" if with_image else "from_frame"
            source = {"kind": "none", "scene_id": None, "asset_id": None}
        else:
            mode = "from_frame"
            source = {"kind": "prev_last_frame", "scene_id": None, "asset_id": None}
        requested = min(max(float(s["requested_s"]), duration.MIN_SECONDS), duration.MAX_SECONDS)
        scenes.append({
            "scene_id": None, "mode": mode,
            "action_text": _with_portraits(s["action"].strip(),
                                           [by_tag[t]["portrait_phrase"] for t in tags]),
            "performance_text": s["performance"].strip(), "sound_text": s["sound"].strip(),
            "music_text": s["music"].strip(), "state_in_text": s["state_in"].strip(),
            "state_out_text": s["state_out"].strip(), "requested_s": requested, "seed": None,
            "source_frame": source, "refs": tags,
        })
        try:
            schemas.SceneIn.model_validate(scenes[-1])
        except ValidationError as err:
            loc = err.errors()[0]["loc"]
            raise StudioError(502, "llm_failed", "Модель ответила не по схеме",
                              {"schema": "SceneIn",
                               "path": "/".join(["", "scenes", str(i), *map(str, loc)])}) from None
    llm_json = {"provider": llm.label, "prompt_version": prompt_version(), "raw": raw}
    return scenes, llm_json, dropped
