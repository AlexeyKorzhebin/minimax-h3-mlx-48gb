"""Slice S1 step 3: the scenarist proposes (O25), the owner applies (O26)."""
from __future__ import annotations

import copy
import io
import json
import sqlite3

import pytest
from _studio_fixtures import TINY_PNG, ScriptedLlm, make_client

from h3_48gb.studio import ops

# Expected values are literals here, never imported from the code under test (review r2, Н2):
# a change to the prompt or the schema must be made twice — in the code and here, on purpose.
SCENARIST_PROMPT = (
    'You are the scenarist of a short video made of consecutive scenes, each scene rendered by the\n'
    'MiniMax H3 video model as its own 3–15 second clip. Scene N+1 starts from the last frame of\n'
    'scene N, so the end of one scene is the beginning of the next.\n'
    '\n'
    'Input: a JSON object with\n'
    '- `idea` — what the owner wants, usually in Russian;\n'
    '- `scenes_count` — exactly how many scenes to write (1 to 12 in one answer);\n'
    '- `frame_format` — "16:9" or "9:16";\n'
    '- `style_block` — visual style to respect (may be empty);\n'
    '- `cast` — characters: `tag`, `description_en`, `portrait_phrase`.\n'
    '\n'
    'Answer with one JSON object `{"scenes": [...]}` holding exactly `scenes_count` scenes, in play\n'
    'order. Every field is English (no Cyrillic). For each scene:\n'
    '- `action` — what happens in the frame: who stands or sits where and on what, what they do,\n'
    "  where the camera is. Do not repeat the character's portrait: the code pastes it in. Never\n"
    '  name an absent character, even in a negation. Never write "@tag" or "mode:".\n'
    '- `performance` — the acting: the inner state at the start, how and when it changes, the face,\n'
    '  the gaze, the hands, small movements. A scene without a change of state reads as a dead face.\n'
    '- `sound` — what sounds, described physically: the material and the contact ("bare heels on\n'
    '  wet planks", "linen brushing against reeds") and the loudness that the gait gives. Say what\n'
    '  does not sound only after what does.\n'
    '- `music` — non-diegetic music: instruments, tempo, dynamics, or "none".\n'
    '- `state_in` — one sentence: the frame at the first moment of the scene.\n'
    '- `state_out` — one sentence: the frame at the last moment; the next scene starts from it.\n'
    '- `cast_tags` — the `tag` of every character from `cast` who is in the frame. The owner cast\n'
    '  them for this video: the opening scene shows them unless the idea explicitly keeps them out\n'
    '  of frame. An empty list means "no one from the cast is visible" — say so in `action`.\n'
    '- `requested_s` — length in seconds, 3 to 15; 8 is a good default.\n'
    '\n'
    'Do not count above three in the action: say "several" instead of a number.\n'
)
SCENARIST_PROMPT_VERSION = "855cc46f8c89"     # sha256(SCENARIST_PROMPT)[:12]
_STR = {"type": "string"}
SCENES_SCHEMA = {
    "name": "studio_scenes",
    "schema": {
        "type": "object",
        "properties": {"scenes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "action": _STR, "performance": _STR, "sound": _STR, "music": _STR,
                "state_in": _STR, "state_out": _STR,
                "cast_tags": {"type": "array", "items": _STR},
                "requested_s": {"type": "number"},
            },
            "required": ["action", "performance", "sound", "music", "state_in", "state_out",
                         "cast_tags", "requested_s"],
            "additionalProperties": False,
        }}},
        "required": ["scenes"],
        "additionalProperties": False,
    },
}

PHRASE = "a woman in her thirties with a loose dark braid"


def _llm_scene(i, tags=("anna",), seconds=8.0):
    return {"action": f"She walks to the lake, shot {i}.",
            "performance": f"Her gaze searches the trees, then her shoulders drop ({i}).",
            "sound": f"Bare heels on wet planks ({i}).", "music": f"A low cello ({i}).",
            "state_in": f"in {i}", "state_out": f"out {i}", "cast_tags": list(tags),
            "requested_s": seconds}


@pytest.fixture
def seeds(monkeypatch):
    it = iter(range(500, 600))
    monkeypatch.setattr(ops, "_new_seed", lambda: next(it))


def _project_with_hero(client, with_image=True):
    p = client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9",
                                                  "idea_text": "девушка идёт к озеру"}).json()
    assets = []
    if with_image:
        a = client.post("/api/studio/assets", files={"file": ("h.png", io.BytesIO(TINY_PNG))}).json()
        assets = [{"asset_id": a["id"], "role": "image"}]
    client.post(f"/api/studio/projects/{p['id']}/cast", json={
        "expected_revision": 1, "tag": "anna", "description_ru": "ж", "assets": assets,
        "description_en": "A woman in her thirties.", "portrait_phrase": PHRASE})
    return p["id"]


def _proposed(i, *, mode, source, seconds):
    return {"scene_id": None, "mode": mode,
            "action_text": f"{PHRASE}. She walks to the lake, shot {i}.",
            "performance_text": f"Her gaze searches the trees, then her shoulders drop ({i}).",
            "sound_text": f"Bare heels on wet planks ({i}).", "music_text": f"A low cello ({i}).",
            "state_in_text": f"in {i}", "state_out_text": f"out {i}", "requested_s": seconds,
            "seed": None, "source_frame": {"kind": source, "scene_id": None, "asset_id": None},
            "refs": ["anna"]}


def test_the_expected_prompt_version_is_the_hash_of_the_expected_prompt():
    import hashlib
    assert hashlib.sha256(SCENARIST_PROMPT.encode()).hexdigest()[:12] == SCENARIST_PROMPT_VERSION


def test_propose_then_apply_gives_two_chained_scenes(tmp_path, seeds):
    raw = {"scenes": [_llm_scene(1), _llm_scene(2, tags=("anna", "@anna"), seconds=4.8125)]}
    llm = ScriptedLlm([copy.deepcopy(raw)])
    client = make_client(tmp_path, llm=llm)
    pid = _project_with_hero(client)
    r = client.post(f"/api/studio/projects/{pid}/director/propose",
                    json={"idea_text": "девушка идёт к озеру", "scenes_count": 2})
    assert r.status_code == 200
    proposal = r.json()
    (call,) = llm.calls
    assert call["schema"] == SCENES_SCHEMA
    assert call["messages"] == [
        {"role": "system", "content": SCENARIST_PROMPT},
        {"role": "user", "content": (
            '{"idea": "девушка идёт к озеру", "scenes_count": 2, "frame_format": "16:9", '
            '"style_block": "", "cast": [{"tag": "anna", "description_en": '
            '"A woman in her thirties.", "portrait_phrase": "' + PHRASE + '"}]}')}]
    assert proposal == {
        "proposal_id": proposal["proposal_id"],
        "scenes": [_proposed(1, mode="refs", source="none", seconds=8.0),
                   _proposed(2, mode="from_frame", source="prev_last_frame", seconds=4.8125)],
        "durations": [{"frames": 192, "duration_s": 8.0}, {"frames": 124, "duration_s": 5.167}],
        "dropped_tags": []}
    row = sqlite3.connect(tmp_path / "out" / "studio" / "studio.sqlite").execute(
        "SELECT llm_json, applied_at FROM proposal").fetchone()
    assert (json.loads(row[0]), row[1]) == (
        {"provider": "scripted", "prompt_version": SCENARIST_PROMPT_VERSION, "raw": raw}, None)
    assert client.get(f"/api/studio/projects/{pid}").json()["scenes"] == []

    applied = client.post(f"/api/studio/projects/{pid}/director/apply",
                          json={"proposal_id": proposal["proposal_id"], "expected_revision": 2})
    assert applied.status_code == 200
    s1, s2 = applied.json()["scenes"]
    assert [(s["idx"], s["mode"], s["frames"], s["seed"], s["refs"], s["sound_text"],
             s["music_text"]) for s in (s1, s2)] == [
        (0, "refs", 192, 500, ["anna"], "Bare heels on wet planks (1).", "A low cello (1)."),
        (1, "from_frame", 124, 501, ["anna"], "Bare heels on wet planks (2).", "A low cello (2).")]
    assert s2["action_text"] == f"{PHRASE}. She walks to the lake, shot 2."
    assert s2["source_frame"] == {"kind": "prev_last_frame", "scene_id": s1["id"], "asset_id": None,
                                  "auto": True}


def test_first_scene_without_a_hero_picture_waits_for_a_keyframe(tmp_path, seeds):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1)]}]))
    pid = _project_with_hero(client, with_image=False)
    (scene,) = client.post(f"/api/studio/projects/{pid}/director/propose",
                           json={"idea_text": "x", "scenes_count": 1}).json()["scenes"]
    assert (scene["mode"], scene["refs"], scene["source_frame"]["kind"]) == \
        ("from_frame", ["anna"], "none")


def test_the_opening_scene_with_an_empty_cast_list_shows_the_pictured_cast(tmp_path, seeds):
    llm = ScriptedLlm([{"scenes": [_llm_scene(1, tags=()), _llm_scene(2, tags=("anna",))]}])
    client = make_client(tmp_path, llm=llm)
    pid = _project_with_hero(client)                       # anna has a picture
    s1, s2 = client.post(f"/api/studio/projects/{pid}/director/propose",
                         json={"idea_text": "x", "scenes_count": 2}).json()["scenes"]
    assert ((s1["mode"], s1["refs"]), s1["action_text"].startswith(PHRASE)) == (("refs", ["anna"]), True)
    assert (s2["mode"], s2["refs"]) == ("from_frame", ["anna"])


def test_wrong_number_of_scenes_is_502_and_no_proposal(tmp_path, seeds):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1)]}]))
    pid = _project_with_hero(client)
    r = client.post(f"/api/studio/projects/{pid}/director/propose",
                    json={"idea_text": "x", "scenes_count": 2})
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["detail"]) == \
        (502, "llm_failed", {"got": 1, "wanted": 2})


@pytest.mark.parametrize("reply, path", [
    ({}, "/"),
    ({"scenes": [{k: v for k, v in _llm_scene(1).items() if k != "sound"}]}, "/scenes/0"),
    ({"scenes": [{**_llm_scene(1), "requested_s": "8"}]}, "/scenes/0/requested_s"),
    ({"scenes": [{**_llm_scene(1), "cast_tags": "anna"}]}, "/scenes/0/cast_tags"),
])
def test_a_reply_off_the_schema_is_a_named_502_not_a_500(tmp_path, seeds, reply, path):
    """Review finding 6: json_schema routes are not checked by the provider layer."""
    client = make_client(tmp_path, llm=ScriptedLlm([reply]))
    pid = _project_with_hero(client)
    r = client.post(f"/api/studio/projects/{pid}/director/propose",
                    json={"idea_text": "x", "scenes_count": 1})
    assert r.status_code == 502
    assert (r.json()["error"]["message"], r.json()["error"]["detail"]["path"]) == \
        ("Модель ответила не по схеме", path)


def test_unknown_tags_are_dropped_and_reported(tmp_path, seeds):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1, ("ghost", "anna"))]}]))
    pid = _project_with_hero(client)
    body = client.post(f"/api/studio/projects/{pid}/director/propose",
                       json={"idea_text": "x", "scenes_count": 1}).json()
    assert (body["scenes"][0]["refs"], body["dropped_tags"]) == (["anna"], ["ghost"])


def test_more_than_twelve_scenes_is_the_scenarists_limit_not_the_projects(tmp_path, seeds):
    llm = ScriptedLlm([])
    client = make_client(tmp_path, llm=llm)
    pid = _project_with_hero(client)
    r = client.post(f"/api/studio/projects/{pid}/director/propose",
                    json={"idea_text": "x", "scenes_count": 13})
    assert r.status_code == 400
    assert r.json()["error"] == {
        "code": "too_many_scenes_for_scenarist",
        "message": "Сценарист пишет не больше 12 сцен за раз; остальные добавьте «+ Сцена»",
        "detail": {"max": 12, "wanted": 13}}
    assert llm.calls == []


def test_applying_twice_is_409_and_the_proposal_survives_a_restart(tmp_path, seeds):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1)]}]))
    pid = _project_with_hero(client)
    proposal_id = client.post(f"/api/studio/projects/{pid}/director/propose",
                              json={"idea_text": "x", "scenes_count": 1}).json()["proposal_id"]
    restarted = make_client(tmp_path, llm=ScriptedLlm([]))
    ok = restarted.post(f"/api/studio/projects/{pid}/director/apply",
                        json={"proposal_id": proposal_id, "expected_revision": 2})
    assert ok.status_code == 200
    again = restarted.post(f"/api/studio/projects/{pid}/director/apply",
                           json={"proposal_id": proposal_id, "expected_revision": 3})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "proposal_already_applied")


def test_a_proposal_survives_a_revision_conflict_and_applies_with_the_fresh_revision(tmp_path, seeds):
    """Review Focus 3: the owner added a hero after proposing; the stale apply must not consume it."""
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1)]}]))
    pid = _project_with_hero(client)
    proposal_id = client.post(f"/api/studio/projects/{pid}/director/propose",
                              json={"idea_text": "x", "scenes_count": 1}).json()["proposal_id"]
    client.post(f"/api/studio/projects/{pid}/cast", json={
        "expected_revision": 2, "tag": "boris", "description_ru": "м",
        "description_en": "A man.", "portrait_phrase": "a tall man"})
    stale = client.post(f"/api/studio/projects/{pid}/director/apply",
                        json={"proposal_id": proposal_id, "expected_revision": 2})
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "revision_conflict")
    fresh = client.post(f"/api/studio/projects/{pid}/director/apply",
                        json={"proposal_id": proposal_id, "expected_revision": 3})
    assert fresh.status_code == 200


def test_prompt_version_follows_the_prompt_file(tmp_path, monkeypatch):
    """The version written into proposal.llm_json must change when the prompt changes."""
    from h3_48gb.studio.director import scenarist
    other = tmp_path / "scenarist.md"
    other.write_text("another prompt\n", encoding="utf-8")
    monkeypatch.setattr(scenarist, "PROMPT", other)
    assert scenarist.prompt_version() == "8fc954fcad67"


def test_the_saved_proposal_carries_the_version_of_the_prompt_actually_used(tmp_path, seeds,
                                                                            monkeypatch):
    """Review r3: not the hash function alone — O25 must WRITE the version of the prompt file it
    sent. With another prompt file the stored version is that file's hash (literal)."""
    from h3_48gb.studio.director import scenarist
    other = tmp_path / "scenarist.md"
    other.write_text("another prompt\n", encoding="utf-8")
    monkeypatch.setattr(scenarist, "PROMPT", other)
    llm = ScriptedLlm([{"scenes": [_llm_scene(1)]}])
    client = make_client(tmp_path, llm=llm)
    pid = _project_with_hero(client)
    client.post(f"/api/studio/projects/{pid}/director/propose",
                json={"idea_text": "x", "scenes_count": 1})
    stored = json.loads(sqlite3.connect(tmp_path / "out" / "studio" / "studio.sqlite").execute(
        "SELECT llm_json FROM proposal").fetchone()[0])
    assert (stored["prompt_version"], llm.calls[0]["messages"][0]["content"]) == (
        "8fc954fcad67", "another prompt\n")



def test_applying_a_proposal_over_a_scene_edited_elsewhere_is_409(tmp_path, seeds):
    """Review r7 blocker for O26: the proposal replaces the whole set — the tab sends the
    versions it read; an O8 in another tab in between stops the replacement."""
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1)]}]))
    pid = _project_with_hero(client)
    (old,) = client.put(f"/api/studio/projects/{pid}/scenes", json={
        "expected_revision": 2, "scenes": [{"mode": "from_frame", "requested_s": 8.0,
                                            "refs": []}]}).json()["scenes"]
    proposal_id = client.post(f"/api/studio/projects/{pid}/director/propose",
                              json={"idea_text": "x", "scenes_count": 1}).json()["proposal_id"]
    client.patch(f"/api/studio/scenes/{old['id']}",
                 json={"expected_edit_version": 0, "music_text": "A cello, kept."})
    stale = client.post(f"/api/studio/projects/{pid}/director/apply", json={
        "proposal_id": proposal_id, "expected_revision": 3,
        "deleted": [{"scene_id": old["id"], "expected_edit_version": 0}]})
    assert (stale.status_code, stale.json()["error"]["detail"]["scene_id"]) == (409, old["id"])
    scenes = client.get(f"/api/studio/projects/{pid}").json()["scenes"]
    assert [(x["id"], x["music_text"]) for x in scenes] == [(old["id"], "A cello, kept.")]
    silent = client.post(f"/api/studio/projects/{pid}/director/apply", json={
        "proposal_id": proposal_id, "expected_revision": 3})
    assert silent.status_code == 409                       # replacing without naming the old set
    fresh = client.post(f"/api/studio/projects/{pid}/director/apply", json={
        "proposal_id": proposal_id, "expected_revision": 3,
        "deleted": [{"scene_id": old["id"], "expected_edit_version": 1}]})
    assert (fresh.status_code, fresh.json()["diff"]["deleted"]) == (200, [old["id"]])


# --- review r1 ----------------------------------------------------------------------------------

def _db(tmp_path):
    c = sqlite3.connect(tmp_path / "out" / "studio" / "studio.sqlite")
    c.row_factory = sqlite3.Row
    return c


def _events(tmp_path, kind):
    return [(r["scene_id"], json.loads(r["payload_json"])) for r in _db(tmp_path).execute(
        "SELECT scene_id, payload_json FROM event WHERE kind = ? ORDER BY seq", (kind,))]


def _propose(client, pid, n=1):
    return client.post(f"/api/studio/projects/{pid}/director/propose",
                       json={"idea_text": "x", "scenes_count": n})


def test_requested_seconds_outside_3_to_15_are_clamped_not_refused(tmp_path, seeds):
    reply = {"scenes": [_llm_scene(1, seconds=16), _llm_scene(2, seconds=1)]}
    client = make_client(tmp_path, llm=ScriptedLlm([reply]))
    pid = _project_with_hero(client)
    r = _propose(client, pid, 2)
    assert r.status_code == 200
    assert ([s["requested_s"] for s in r.json()["scenes"]], r.json()["durations"]) == (
        [15.0, 3.0], [{"frames": 345, "duration_s": 14.375}, {"frames": 73, "duration_s": 3.042}])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_length_is_a_named_502_and_leaves_nothing_behind(tmp_path, seeds, bad):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1, seconds=bad)]}]))
    pid = _project_with_hero(client)
    r = _propose(client, pid)
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["detail"]["path"]) == (
        502, "llm_failed", "/scenes/0/requested_s")
    assert _db(tmp_path).execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 0
    assert _events(tmp_path, "proposal_created") == []


def test_a_scene_the_scene_schema_refuses_is_a_named_502(tmp_path, seeds):
    """Review r1 minor 4: every scene goes through SceneIn before it is saved. A scene text
    over the 8000-character limit cannot be applied later, so they are refused now."""
    scene = {**_llm_scene(1), "sound": "x" * 9000}
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [scene]}]))
    pid = _project_with_hero(client)
    r = _propose(client, pid)
    assert (r.status_code, r.json()["error"]["detail"]["path"]) == (502, "/scenes/0/sound_text")
    assert _db(tmp_path).execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 0


def test_a_proposal_of_another_project_is_404(tmp_path, seeds):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1)]}]))
    pid = _project_with_hero(client)
    # The same cast in both projects: only the project check can refuse (re-review r1, M1).
    other = _project_with_hero(client)
    proposal_id = _propose(client, pid).json()["proposal_id"]
    r = client.post(f"/api/studio/projects/{other}/director/apply",
                    json={"proposal_id": proposal_id, "expected_revision": 2})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "proposal_not_found")


@pytest.mark.parametrize("action, expected", [
    (f"{PHRASE} walks to the lake.", f"{PHRASE} walks to the lake."),          # already first
    (f"She stops. {PHRASE} looks up.", f"{PHRASE}. She stops. looks up."),    # inside -> front
    ("She walks.", f"{PHRASE}. She walks."),
])
def test_the_portrait_phrase_stands_once_at_the_start(tmp_path, seeds, action, expected):
    scene = {**_llm_scene(1), "action": action}
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [scene]}]))
    pid = _project_with_hero(client)
    (got,) = _propose(client, pid).json()["scenes"]
    assert got["action_text"] == expected


def test_dropped_tags_are_unique_name_shaped_and_capped(tmp_path, seeds):
    tags = ["ghost", "@ghost", "Ghost", "not a tag!", "x" * 40, *[f"g{i}" for i in range(20)]]
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1, tuple(tags))]}]))
    pid = _project_with_hero(client)
    dropped = _propose(client, pid).json()["dropped_tags"]
    assert dropped == ["ghost", *[f"g{i}" for i in range(11)]]


def test_tags_match_without_regard_to_case(tmp_path, seeds):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1, ("@ANNA",))]}]))
    pid = _project_with_hero(client)
    body = _propose(client, pid).json()
    assert (body["scenes"][0]["refs"], body["dropped_tags"]) == (["anna"], [])


def test_refs_mode_needs_a_hero_on_screen_who_has_a_picture(tmp_path, seeds):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1, ("boris",))]}]))
    pid = _project_with_hero(client)                     # anna has a picture
    client.post(f"/api/studio/projects/{pid}/cast", json={
        "expected_revision": 2, "tag": "boris", "description_ru": "м",
        "description_en": "A man.", "portrait_phrase": "a tall man"})
    (scene,) = _propose(client, pid).json()["scenes"]
    assert (scene["mode"], scene["refs"]) == ("from_frame", ["boris"])


def test_the_events_and_the_row_carry_exact_content(tmp_path, seeds):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1), _llm_scene(2)]}]))
    pid = _project_with_hero(client)
    proposal_id = _propose(client, pid, 2).json()["proposal_id"]
    assert _events(tmp_path, "proposal_created") == [
        (None, {"proposal_id": proposal_id, "scenes": 2})]
    assert _db(tmp_path).execute("SELECT idea_text FROM proposal").fetchone()[0] == "x"
    r = client.post(f"/api/studio/projects/{pid}/director/apply",
                    json={"proposal_id": proposal_id, "expected_revision": 2})
    assert _events(tmp_path, "proposal_applied") == [
        (None, {"proposal_id": proposal_id, "diff": r.json()["diff"]})]
    assert _events(tmp_path, "scenes_replaced") == []


def test_twelve_scenes_pass_and_thirteen_do_not(tmp_path, seeds):
    llm = ScriptedLlm([{"scenes": [_llm_scene(i) for i in range(12)]}])
    client = make_client(tmp_path, llm=llm)
    pid = _project_with_hero(client)
    assert (_propose(client, pid, 12).status_code,
            _propose(client, pid, 13).status_code) == (200, 400)


def test_the_projects_style_block_reaches_the_model(tmp_path, seeds):
    llm = ScriptedLlm([{"scenes": [_llm_scene(1)]}])
    client = make_client(tmp_path, llm=llm)
    pid = _project_with_hero(client)
    c = _db(tmp_path)
    c.execute("UPDATE project SET style_block = ? WHERE id = ?", ("soft overcast light", pid))
    c.commit()
    _propose(client, pid)
    assert json.loads(llm.calls[0]["messages"][1]["content"])["style_block"] == "soft overcast light"


def test_a_proposal_consumed_between_the_check_and_the_write_is_409_and_changes_nothing(
        tmp_path, seeds, monkeypatch):
    client = make_client(tmp_path, llm=ScriptedLlm([{"scenes": [_llm_scene(1)]}]))
    pid = _project_with_hero(client)
    proposal_id = _propose(client, pid).json()["proposal_id"]
    real = ops.replace_scenes

    def raced(conn, *a, **kw):
        other = sqlite3.connect(tmp_path / "out" / "studio" / "studio.sqlite")
        other.execute("UPDATE proposal SET applied_at = 'earlier' WHERE id = ?", (proposal_id,))
        other.commit()
        other.close()
        return real(conn, *a, **kw)

    monkeypatch.setattr(ops, "replace_scenes", raced)
    r = client.post(f"/api/studio/projects/{pid}/director/apply",
                    json={"proposal_id": proposal_id, "expected_revision": 2})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "proposal_already_applied")
    assert client.get(f"/api/studio/projects/{pid}").json()["scenes"] == []
    assert _db(tmp_path).execute("SELECT applied_at FROM proposal").fetchone()[0] == "earlier"
