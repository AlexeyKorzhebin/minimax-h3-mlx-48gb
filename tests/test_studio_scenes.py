"""Slice S1 scenes: O7 matching by scene_id, O8 edits, И1 revisions, 409 revision_conflict."""
from __future__ import annotations

import json

import pytest
from _studio_fixtures import ScriptedLlm, make_client

from h3_48gb.studio import ops

ACTION = ("A woman walks out from the birches onto a forest path, stops at the edge of the water "
          "and looks across the lake toward the far shore while the morning mist lifts slowly "
          "above the reeds.")
PERF = ("Her gaze searches the gaps between the trees, then her shoulders drop and her fingers "
        "loosen around the strap of her bag.")


def _scene(**over):
    base = {"mode": "from_frame", "action_text": ACTION, "performance_text": PERF,
            "sound_text": "Soft footsteps on damp grass.", "music_text": "none",
            "requested_s": 8.0, "source_frame": {"kind": "none"}, "refs": []}
    return {**base, **over}


@pytest.fixture
def client(tmp_path, monkeypatch):
    seeds = iter(range(101, 200))
    monkeypatch.setattr(ops, "_new_seed", lambda: next(seeds))
    return make_client(tmp_path, llm=ScriptedLlm([]))


def _project(client):
    return client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"}).json()


def _put(client, pid, rev, scenes, deleted=()):
    """O7; `deleted` — the scenes this tab removes, as {scene_id, expected_edit_version}."""
    return client.put(f"/api/studio/projects/{pid}/scenes",
                      json={"expected_revision": rev, "scenes": scenes, "deleted": list(deleted)})


def _gone(*scenes):
    return [{"scene_id": s["id"], "expected_edit_version": s["edit_version"]} for s in scenes]


def _with_cast(client, pid, rev=1, tag="anna"):
    client.post(f"/api/studio/projects/{pid}/cast", json={
        "expected_revision": rev, "tag": tag, "description_ru": "ж",
        "description_en": "A woman.", "portrait_phrase": "a woman with a braid"})


def test_put_creates_scenes_in_array_order_with_derived_sources(client):
    p = _project(client)
    r = _put(client, p["id"], 1, [_scene(), _scene(source_frame={"kind": "prev_last_frame"},
                                               requested_s=4.8125)])
    assert r.status_code == 200
    body = r.json()
    one, two = body["scenes"]
    assert body["project"]["revision"] == 2
    assert body["diff"] == {"added": [one["id"], two["id"]], "unchanged": [], "changed": [],
                            "moved": [], "deleted": []}
    assert [(s["idx"], s["revision"], s["frames"], s["duration_s"], s["seed"]) for s in (one, two)] \
        == [(0, 1, 192, 8.0, 101), (1, 1, 124, 5.167, 102)]
    assert two["source_frame"] == {"kind": "prev_last_frame", "scene_id": one["id"],
                                   "asset_id": None, "auto": True}
    assert one["source_frame"] == {"kind": "none", "scene_id": None, "asset_id": None,
                                   "auto": False}
    assert client.get(f"/api/studio/projects/{p['id']}").json()["scenes"] == body["scenes"]


def test_put_matches_by_id_moves_changes_and_soft_deletes(client):
    p = _project(client)
    a, b, c = _put(client, p["id"], 1, [_scene(), _scene(action_text=ACTION + " B"),
                                        _scene(action_text=ACTION + " C")]).json()["scenes"]
    # new order: C, A(edited), new; B deleted
    r = _put(client, p["id"], 2, [
        {**_scene(action_text=ACTION + " C"), "scene_id": c["id"], "expected_edit_version": c["edit_version"]},
        {**_scene(action_text=ACTION + " A2"), "scene_id": a["id"], "expected_edit_version": a["edit_version"]},
        _scene(action_text=ACTION + " D")], _gone(b)).json()
    new_id = r["scenes"][2]["id"]
    assert r["diff"] == {"added": [new_id], "unchanged": [], "changed": [a["id"]],
                         "moved": [c["id"]], "deleted": [b["id"]]}
    assert [(s["id"], s["idx"], s["revision"], s["seed"]) for s in r["scenes"]] == [
        (c["id"], 0, 1, c["seed"]), (a["id"], 1, 2, a["seed"]), (new_id, 2, 1, 104)]


def test_reorder_that_changes_the_frame_source_is_a_content_change(client):
    p = _project(client)
    a, b, c = _put(client, p["id"], 1, [
        _scene(), _scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " B"),
        _scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " C")]).json()["scenes"]
    r = _put(client, p["id"], 2, [
        {**_scene(), "scene_id": a["id"], "expected_edit_version": a["edit_version"]},
        {**_scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " C"),
         "scene_id": c["id"], "expected_edit_version": c["edit_version"]},
        {**_scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " B"),
         "scene_id": b["id"], "expected_edit_version": b["edit_version"]}]).json()
    assert r["diff"]["changed"] == [c["id"], b["id"]]
    assert r["diff"]["unchanged"] == [a["id"]]
    assert [(s["id"], s["revision"], s["source_frame"]["scene_id"]) for s in r["scenes"]] == [
        (a["id"], 1, None), (c["id"], 2, a["id"]), (b["id"], 2, c["id"])]


def test_put_with_an_unknown_scene_id_writes_nothing(client):
    p = _project(client)
    r = _put(client, p["id"], 1, [{**_scene(), "scene_id": "s_nope"}])
    assert (r.status_code, r.json()["error"]["code"]) == (400, "scene_unknown")
    snap = client.get(f"/api/studio/projects/{p['id']}").json()
    assert (snap["scenes"], snap["project"]["revision"], snap["last_seq"]) == ([], 1, 1)


def test_put_with_a_stale_project_revision_is_409(client):
    p = _project(client)
    _put(client, p["id"], 1, [_scene()])
    r = _put(client, p["id"], 1, [_scene()])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "revision_conflict")
    assert r.json()["error"]["detail"]["current"]["revision"] == 2


@pytest.mark.parametrize("field, value, stored", [
    ("mode", "refs", {"mode": "refs"}),
    ("action_text", ACTION + " Then she smiles.", {"action_text": ACTION + " Then she smiles."}),
    ("performance_text", PERF + " Then she laughs.", {"performance_text": PERF + " Then she laughs."}),
    ("sound_text", "Wind in the reeds.", {"sound_text": "Wind in the reeds."}),
    ("music_text", "A single cello.", {"music_text": "A single cello."}),
    ("requested_s", 10.0, {"requested_s": 10.0, "frames": 243, "duration_s": 10.125}),
    ("seed", 7, {"seed": 7}),
    ("source_frame", {"kind": "prev_last_frame"},
     {"source_frame": {"kind": "prev_last_frame", "scene_id": "<first>", "asset_id": None,
                       "auto": True}}),
    ("refs", ["anna"], {"refs": ["anna"]}),
])
def test_each_fingerprint_field_bumps_the_revision_and_is_stored(client, field, value, stored):
    """И1: +1 revision, the new value in the answer AND on a fresh read, project untouched."""
    p = _project(client)
    _with_cast(client, p["id"])
    first, second = _put(client, p["id"], 2, [_scene(), _scene()]).json()["scenes"]
    expected = {**second, **json.loads(json.dumps(stored).replace("<first>", first["id"])),
                "revision": 2, "edit_version": 1}
    r = client.patch(f"/api/studio/scenes/{second['id']}",
                     json={"expected_edit_version": 0, field: value})
    assert r.status_code == 200
    assert r.json()["scene"] == expected
    snap = client.get(f"/api/studio/projects/{p['id']}").json()
    assert snap["scenes"][1] == expected
    assert snap["project"]["revision"] == 3


@pytest.mark.parametrize("field, value", [
    ("state_in_text", "She stands at the birches."), ("state_out_text", "She faces the lake."),
    ("requested_s", 8.1),        # same 192 frames: requested_s alone is not in the fingerprint
])
def test_non_fingerprint_edits_keep_the_revision_and_bump_the_edit_version(client, field, value):
    """И1: revision untouched; И1a: edit_version +1 all the same — exact values."""
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    r = client.patch(f"/api/studio/scenes/{scene['id']}",
                     json={"expected_edit_version": 0, field: value})
    got = r.json()["scene"]
    assert (got["revision"], got["edit_version"], got[field]) == (1, 1, value)
    same = client.patch(f"/api/studio/scenes/{scene['id']}",
                        json={"expected_edit_version": 1, field: value}).json()["scene"]
    assert (same["revision"], same["edit_version"]) == (1, 1)     # nothing changed, no bump


def test_project_title_change_does_not_touch_scene_revisions(client):
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    client.patch(f"/api/studio/projects/{p['id']}", json={"expected_revision": 2, "title": "Б"})
    assert client.get(f"/api/studio/projects/{p['id']}").json()["scenes"][0]["revision"] == 1


def test_patch_with_a_stale_scene_revision_is_409_with_the_current_scene(client):
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    ok = client.patch(f"/api/studio/scenes/{scene['id']}", json={"expected_edit_version": 0, "action_text": ACTION + " X"}).json()["scene"]
    r = client.patch(f"/api/studio/scenes/{scene['id']}", json={"expected_edit_version": 0, "action_text": ACTION + " Y"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "revision_conflict"
    assert r.json()["error"]["detail"]["current"] == ok


def test_patch_returns_this_scenes_lint_and_an_empty_cascade(client):
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    r = client.patch(f"/api/studio/scenes/{scene['id']}", json={"expected_edit_version": 0, "sound_text": "Шаги по траве."}).json()
    assert r["cascade"] == []
    assert [(f["rule"], f["field"]) for f in r["lint"]] == [("L4", "sound_text")]


def test_patch_of_a_deleted_scene_is_404(client):
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    _put(client, p["id"], 2, [], _gone(scene))
    r = client.patch(f"/api/studio/scenes/{scene['id']}", json={"expected_edit_version": 0, "seed": 3})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "scene_not_found")


def test_patch_of_a_missing_scene_is_404_before_the_body_is_read(client):
    r = client.patch("/api/studio/scenes/s_nope", json={"seed": "мусор", "refs": 5})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "scene_not_found")


def test_unknown_cast_tag_in_refs_is_400(client):
    p = _project(client)
    r = _put(client, p["id"], 1, [_scene(mode="refs", refs=["ghost"])])
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["detail"]) == \
        (400, "cast_unknown", {"tag": "ghost"})


def test_deleting_the_first_scene_of_a_chain_leaves_an_l12_error_not_a_crash(client):
    """Review Focus 2."""
    p = _project(client)
    a, b = _put(client, p["id"], 1, [_scene(), _scene(source_frame={"kind": "prev_last_frame"})]
                ).json()["scenes"]
    r = _put(client, p["id"], 2, [{**_scene(source_frame={"kind": "prev_last_frame"}),
                                   "scene_id": b["id"], "expected_edit_version": b["edit_version"]}],
             _gone(a))
    assert r.status_code == 200
    (only,) = r.json()["scenes"]
    assert (only["idx"], only["revision"], only["source_frame"]) == (
        0, 2, {"kind": "prev_last_frame", "scene_id": None, "asset_id": None, "auto": True})
    assert [(f["rule"], f["severity"]) for f in r.json()["lint"][b["id"]]] == [("L12", "error")]


def test_two_tabs_editing_two_scenes_both_save(client):
    """Review Focus 4: O8 does not touch the project revision, so one scene never blocks another."""
    p = _project(client)
    a, b = _put(client, p["id"], 1, [_scene(), _scene()]).json()["scenes"]
    ra = client.patch(f"/api/studio/scenes/{a['id']}", json={"expected_edit_version": 0, "seed": 5})
    rb = client.patch(f"/api/studio/scenes/{b['id']}", json={"expected_edit_version": 0, "seed": 6})
    assert (ra.status_code, rb.status_code) == (200, 200)
    assert client.get(f"/api/studio/projects/{p['id']}").json()["project"]["revision"] == 2


def test_lint_route_returns_findings_by_scene_and_404_for_a_stranger(client):
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene(music_text="Тишина")]).json()["scenes"]
    r = client.post(f"/api/studio/projects/{p['id']}/lint", json={})
    assert r.json() == {"findings": {scene["id"]: [
        {"rule": "L4", "severity": "error", "scene_id": scene["id"], "field": "music_text",
         "message": "Кириллица в поле промпта: H3 ждёт английский"}]}}
    r = client.post(f"/api/studio/projects/{p['id']}/lint", json={"scene_ids": ["s_nope"]})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "scene_not_found")


def test_a_project_has_no_upper_bound_on_scenes(client):
    """Owner 21.10: «+ Сцена» without a ceiling — the 51st and the 60th scene are accepted."""
    p = _project(client)
    r = _put(client, p["id"], 1, [_scene(source_frame={"kind": "prev_last_frame"} if i else
                                         {"kind": "none"}) for i in range(60)])
    assert r.status_code == 200
    assert [s["idx"] for s in r.json()["scenes"]] == list(range(60))


def test_deleting_the_middle_scene_rechains_the_next_one_to_the_previous(client):
    p = _project(client)
    a, b, c = _put(client, p["id"], 1, [
        _scene(), _scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " B"),
        _scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " C")]).json()["scenes"]
    r = _put(client, p["id"], 2, [
        {**_scene(), "scene_id": a["id"], "expected_edit_version": a["edit_version"]},
        {**_scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " C"),
         "scene_id": c["id"], "expected_edit_version": c["edit_version"]}], _gone(b))
    assert [(s["id"], s["revision"], s["source_frame"]["scene_id"]) for s in r.json()["scenes"]] \
        == [(a["id"], 1, None), (c["id"], 2, a["id"])]
    stale = _put(client, p["id"], 3, [
        {**_scene(), "scene_id": a["id"], "expected_edit_version": a["edit_version"]},
        {**_scene(source_frame={"kind": "prev_last_frame", "scene_id": b["id"]}),
         "scene_id": c["id"], "expected_edit_version": 1}])
    assert (stale.status_code, stale.json()["error"]["code"]) == (400, "source_scene_unknown")


def test_switching_the_frame_from_one_asset_to_another_is_a_content_change(client):
    """И1 for `source_frame_asset_id` (review r2, Н2): a new keyframe is a new revision."""
    import io
    from _studio_fixtures import TINY_PNG
    p = _project(client)
    a1, a2 = (client.post("/api/studio/assets",
                          files={"file": ("k.png", io.BytesIO(TINY_PNG))}).json()["id"]
              for _ in range(2))
    (scene,) = _put(client, p["id"], 1, [_scene(source_frame={"kind": "asset", "asset_id": a1})]
                    ).json()["scenes"]
    r = client.patch(f"/api/studio/scenes/{scene['id']}", json={
        "expected_edit_version": 0, "source_frame": {"kind": "asset", "asset_id": a2}})
    assert (r.json()["scene"]["revision"], r.json()["scene"]["source_frame"]) == (
        2, {"kind": "asset", "scene_id": None, "asset_id": a2, "auto": False})
    assert client.get(f"/api/studio/projects/{p['id']}").json()["scenes"][0]["source_frame"] == {
        "kind": "asset", "scene_id": None, "asset_id": a2, "auto": False}


def test_a_branch_of_frame_sources_is_saved_and_linted(client):
    """Review r3 blocker: O8 points scene 3 at scene 1 while scene 2 also continues scene 1."""
    p = _project(client)
    _with_cast(client, p["id"])
    one, two, three = _put(client, p["id"], 2, [
        _scene(refs=["anna"]), _scene(source_frame={"kind": "prev_last_frame"}, refs=["anna"]),
        _scene(source_frame={"kind": "prev_last_frame"}, refs=["anna"])]).json()["scenes"]
    r = client.patch(f"/api/studio/scenes/{three['id']}", json={
        "expected_edit_version": 0, "source_frame": {"kind": "prev_last_frame", "scene_id": one["id"]}})
    assert r.status_code == 200
    assert [f["rule"] for f in r.json()["lint"] if f["rule"] in ("L6", "lint_internal")] == []
    again = client.get(f"/api/studio/projects/{p['id']}").json()["scenes"][2]
    assert (again["revision"], again["source_frame"]["scene_id"]) == (2, one["id"])
    assert client.post(f"/api/studio/projects/{p['id']}/lint", json={}).status_code == 200


def test_a_breaking_rule_after_commit_is_a_finding_not_a_500(client, monkeypatch):
    """The edit is committed before the lint runs: a rule bug must not turn the answer into a
    500 for a change that was saved."""
    from h3_48gb.studio import lint

    def broken(scene, project, limits):
        raise KeyError("boom")
    broken.__name__ = "l6_portrait_drift"
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    monkeypatch.setattr(lint, "RULES", (broken,))
    r = client.patch(f"/api/studio/scenes/{scene['id']}", json={"expected_edit_version": 0, "seed": 9})
    assert r.status_code == 200
    assert r.json()["lint"] == [{"rule": "lint_internal", "severity": "info",
                                 "scene_id": scene["id"], "field": None,
                                 "message": "Правило l6_portrait_drift не смогло проверить сцену "
                                            "(KeyError)"}]
    assert client.get(f"/api/studio/projects/{p['id']}").json()["scenes"][0]["seed"] == 9



#: Every field the owner can edit in a scene, written out here (not taken from ops): a field
#: whose change does not bump edit_version lets a stale tab overwrite another tab's saved value
#: (review r6 blocker). (field, what tab B saves, what stale tab A then sends)
EDITABLE = [
    ("mode", "refs", "text"),
    ("action_text", ACTION + " B.", ACTION + " A."),
    ("performance_text", PERF + " B.", PERF + " A."),
    ("sound_text", "Wind in the reeds.", "Rain on the roof."),
    ("music_text", "A cello.", "A flute."),
    ("state_in_text", "She stands.", "She sits."),
    ("state_out_text", "She leaves.", "She stays."),
    ("requested_s", 8.1, 8.2),                       # same 192 frames: И1 keeps the revision
    ("seed", 7, 9),
    ("source_frame", {"kind": "prev_last_frame"}, {"kind": "none"}),
    ("refs", ["anna"], []),
]


def _value(scene: dict, field: str):
    return scene["source_frame"]["kind"] if field == "source_frame" else scene[field]


@pytest.mark.parametrize("field, b_value, a_value", EDITABLE, ids=[f for f, _, _ in EDITABLE])
def test_a_stale_tab_cannot_overwrite_another_tabs_edit_of_any_field(client, field, b_value,
                                                                     a_value):
    """Tabs A and B read the scene at edit_version 0. B saves; A sends its old copy with the old
    version and gets 409 — the re-read keeps B's value. Then A, having seen it, may overwrite."""
    p = _project(client)
    _with_cast(client, p["id"])
    _, scene = _put(client, p["id"], 2, [_scene(), _scene()]).json()["scenes"]
    b = client.patch(f"/api/studio/scenes/{scene['id']}",
                     json={"expected_edit_version": 0, field: b_value})
    assert (b.status_code, b.json()["scene"]["edit_version"]) == (200, 1)
    a = client.patch(f"/api/studio/scenes/{scene['id']}",
                     json={"expected_edit_version": 0, field: a_value})
    assert a.status_code == 409
    assert a.json()["error"]["code"] == "revision_conflict"
    assert a.json()["error"]["detail"]["current"] == b.json()["scene"]
    kept = client.get(f"/api/studio/projects/{p['id']}").json()["scenes"][1]
    assert _value(kept, field) == _value(b.json()["scene"], field)
    mine = client.patch(f"/api/studio/scenes/{scene['id']}",
                        json={"expected_edit_version": 1, field: a_value})
    assert (mine.status_code, mine.json()["scene"]["edit_version"]) == (200, 2)


def test_o7_with_a_stale_copy_of_a_scene_is_409_and_keeps_the_saved_edit(client):
    """O8 in tab B does not bump the project revision; tab A's «+ Сцена» (O7) still carries its
    old copy of scene 1 — the per-scene expected_edit_version refuses it."""
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    client.patch(f"/api/studio/scenes/{scene['id']}",
                 json={"expected_edit_version": 0, "requested_s": 8.1})
    r = _put(client, p["id"], 2, [{**_scene(), "scene_id": scene["id"], "expected_edit_version": 0},
                                  _scene()])
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["detail"]["scene_id"]) == (
        409, "revision_conflict", scene["id"])
    snap = client.get(f"/api/studio/projects/{p['id']}").json()
    assert ([s["requested_s"] for s in snap["scenes"]], snap["project"]["revision"]) == ([8.1], 2)
    missing = _put(client, p["id"], 2, [{**_scene(), "scene_id": scene["id"]}])
    assert (missing.status_code, missing.json()["error"]["code"]) == (400, "args_invalid")



def test_a_deletion_at_a_stale_version_is_409_and_the_other_tabs_edit_survives(client):
    """Review r7 blocker: A removes S while B saves an edit of S through O8 — A's O7 must not
    make B's saved text disappear. Then A, having seen B's version, may delete."""
    p = _project(client)
    s, t = _put(client, p["id"], 1, [_scene(), _scene()]).json()["scenes"]
    b = client.patch(f"/api/studio/scenes/{s['id']}",
                     json={"expected_edit_version": 0, "state_in_text": "She waits by the door."})
    assert b.json()["scene"]["edit_version"] == 1
    keep_t = [{**_scene(), "scene_id": t["id"], "expected_edit_version": 0}]
    a = _put(client, p["id"], 2, keep_t, _gone(s))                     # A read S at version 0
    assert a.status_code == 409
    assert a.json()["error"]["code"] == "revision_conflict"
    assert a.json()["error"]["detail"] == {"scene_id": s["id"], "current": b.json()["scene"]}
    again = client.get(f"/api/studio/projects/{p['id']}").json()
    assert ([x["id"] for x in again["scenes"]], again["scenes"][0]["state_in_text"],
            again["project"]["revision"]) == ([s["id"], t["id"]], "She waits by the door.", 2)
    ok = _put(client, p["id"], 2, keep_t, [{"scene_id": s["id"], "expected_edit_version": 1}])
    assert (ok.status_code, ok.json()["diff"]["deleted"]) == (200, [s["id"]])


def test_a_live_scene_neither_kept_nor_deleted_is_409(client):
    """The server wants the deletions spelled out: silence about a scene is not consent."""
    p = _project(client)
    s, t = _put(client, p["id"], 1, [_scene(), _scene()]).json()["scenes"]
    r = _put(client, p["id"], 2, [{**_scene(), "scene_id": t["id"], "expected_edit_version": 0}])
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["detail"]["scene_id"]) == (
        409, "revision_conflict", s["id"])
    both = _put(client, p["id"], 2, [{**_scene(), "scene_id": t["id"], "expected_edit_version": 0}],
                _gone(s, t))
    assert (both.status_code, both.json()["error"]["code"]) == (400, "args_invalid")
    stranger = _put(client, p["id"], 2, [], [{"scene_id": "s_nope", "expected_edit_version": 0}])
    assert (stranger.status_code, stranger.json()["error"]["code"]) == (400, "scene_unknown")
    assert [x["id"] for x in client.get(f"/api/studio/projects/{p['id']}").json()["scenes"]] \
        == [s["id"], t["id"]]


# --- review of task 10, round 1: content, not form --------------------------------------------

def _event_rows(tmp_path, kinds=None):
    import sqlite3
    from h3_48gb.studio import events
    conn = sqlite3.connect(tmp_path / "out" / "studio" / "studio.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return [e for e in events.read_after(conn, 0, 1000) if kinds is None or e["kind"] in kinds]
    finally:
        conn.close()


def _keep(scene, **over):
    """The scene as the master sends it back through O7: its id and the version it read."""
    body = {**_scene(action_text=scene["action_text"]), "scene_id": scene["id"],
            "expected_edit_version": scene["edit_version"]}
    return {**body, **over}


#: (field, new value) — one editable field changed through O7, nothing else.
O7_FIELDS = [
    ("mode", "text"), ("action_text", ACTION + " Then she smiles."),
    ("performance_text", PERF + " Then she laughs."), ("sound_text", "Wind in the reeds."),
    ("music_text", "A single cello."), ("state_in_text", "She stands."),
    ("state_out_text", "She leaves."), ("requested_s", 8.1), ("seed", 7),
    ("source_frame", {"kind": "prev_last_frame"}), ("refs", ["anna"]),
]


@pytest.mark.parametrize("field, value", O7_FIELDS, ids=[f for f, _ in O7_FIELDS])
def test_o7_bumps_edit_version_by_exactly_one_for_a_single_field_change(client, field, value):
    """И1a through O7 (review r1, 1): the field the tab changed -> +1 on that scene only; the
    same body again -> no bump (fields written back unchanged do not count)."""
    p = _project(client)
    _with_cast(client, p["id"])
    one, two = _put(client, p["id"], 2, [_scene(), _scene()]).json()["scenes"]
    r = _put(client, p["id"], 3, [_keep(one), _keep(two, **{field: value})]).json()["scenes"]
    assert [s["edit_version"] for s in r] == [0, 1]
    again = _put(client, p["id"], 4, [_keep(r[0]), _keep(r[1], **{field: value})]).json()["scenes"]
    assert [s["edit_version"] for s in again] == [0, 1]
    assert _value(again[1], field) == _value(r[1], field)


def test_o8_scene_updated_event_body_is_exact_and_a_noop_emits_nothing(client, tmp_path):
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    sid = scene["id"]
    client.patch(f"/api/studio/scenes/{sid}", json={"expected_edit_version": 0,
                                                    "state_in_text": "She stands."})
    client.patch(f"/api/studio/scenes/{sid}", json={"expected_edit_version": 1,
                                                    "action_text": ACTION + " X"})
    before = _event_rows(tmp_path)
    client.patch(f"/api/studio/scenes/{sid}", json={"expected_edit_version": 2,
                                                    "action_text": ACTION + " X"})   # no change
    assert _event_rows(tmp_path) == before
    updated = [(e["project_id"], e["scene_id"], e["payload"])
               for e in _event_rows(tmp_path, {"scene_updated"})]
    assert updated == [(p["id"], sid, {"revision": 1, "edit_version": 1,
                                       "fields": ["state_in_text"]}),
                       (p["id"], sid, {"revision": 2, "edit_version": 2,
                                       "fields": ["action_text"]})]


def test_o7_emits_scenes_replaced_with_the_exact_diff(client, tmp_path):
    p = _project(client)
    a, b = _put(client, p["id"], 1, [_scene(), _scene(action_text=ACTION + " B")]).json()["scenes"]
    r = _put(client, p["id"], 2, [_keep(b), _scene(action_text=ACTION + " N")], _gone(a)).json()
    (_, first), = [(e["seq"], e) for e in _event_rows(tmp_path, {"scenes_replaced"})][:1]
    assert first["payload"] == {"diff": {"added": [a["id"], b["id"]], "unchanged": [],
                                         "changed": [], "moved": [], "deleted": []}}
    second = _event_rows(tmp_path, {"scenes_replaced"})[1]
    assert (second["project_id"], second["scene_id"], second["payload"]) == (
        p["id"], None, {"diff": r["diff"]})
    assert r["diff"] == {"added": [r["scenes"][1]["id"]], "unchanged": [], "changed": [],
                         "moved": [b["id"]], "deleted": [a["id"]]}


@pytest.mark.parametrize("method, path, body, loc", [
    ("patch", "/scenes/{sid}", {"expected_edit_version": 0, "requsted_s": 9},
     ["body", "requsted_s"]),
    ("patch", "/scenes/{sid}", {"expected_edit_version": 0,
                                "source_frame": {"kind": "none", "auto": True}},
     ["body", "source_frame", "auto"]),
    ("put", "/projects/{pid}/scenes", {"expected_revision": 2, "scenes": [], "stray": 1},
     ["body", "stray"]),
    ("put", "/projects/{pid}/scenes", {"expected_revision": 2, "scenes": [
        {"mode": "text", "requested_s": 8, "requsted_s": 9}]},
     ["body", "scenes", 0, "requsted_s"]),
    ("put", "/projects/{pid}/scenes", {"expected_revision": 2, "scenes": [
        {"mode": "text", "requested_s": 8, "source_frame": {"kind": "none", "auto": True}}]},
     ["body", "scenes", 0, "source_frame", "auto"]),
    ("put", "/projects/{pid}/scenes", {"expected_revision": 2, "scenes": [], "deleted": [
        {"scene_id": "s", "expected_edit_version": 0, "why": 1}]},
     ["body", "deleted", 0, "why"]),
    ("post", "/projects/{pid}/lint", {"scene_idz": []}, ["body", "scene_idz"]),
], ids=["patch", "patch-source", "put-top", "put-scene", "put-source", "put-deleted", "lint"])
def test_unknown_fields_are_a_400_naming_the_field_and_nothing_is_written(
        client, method, path, body, loc):
    """extra=forbid (review r1, 3): a typo in a field name must not be silently dropped — and
    the `auto` of a source is the server's to set, never the client's."""
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    r = getattr(client, method)("/api/studio" + path.format(sid=scene["id"], pid=p["id"]),
                                json=body)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "args_invalid")
    assert [e["loc"] for e in r.json()["error"]["detail"]["errors"]] == [loc]
    assert client.get(f"/api/studio/projects/{p['id']}").json()["scenes"] == [scene]


def test_seed_bounds_in_o8_and_o7_and_an_explicit_seed_is_kept(client):
    p = _project(client)
    (scene,) = _put(client, p["id"], 1, [_scene()]).json()["scenes"]
    url = f"/api/studio/scenes/{scene['id']}"
    top = client.patch(url, json={"expected_edit_version": 0, "seed": 2**31 - 1})
    assert (top.status_code, top.json()["scene"]["seed"]) == (200, 2**31 - 1)
    for bad in (2**31, 0):
        r = client.patch(url, json={"expected_edit_version": 1, "seed": bad})
        assert (r.status_code, r.json()["error"]["code"]) == (400, "args_invalid")
        r = _put(client, p["id"], 2, [_keep(top.json()["scene"], seed=bad)])
        assert (r.status_code, r.json()["error"]["code"]) == (400, "args_invalid")
    ok = _put(client, p["id"], 2, [_keep(top.json()["scene"], seed=2**31 - 1),
                                   _scene(seed=424242)]).json()["scenes"]
    assert [s["seed"] for s in ok] == [2**31 - 1, 424242]
    moved = _put(client, p["id"], 3, [_keep(ok[0], seed=77), _keep(ok[1])]).json()["scenes"]
    assert [s["seed"] for s in moved] == [77, 424242]       # given seed wins, absent one is kept


def test_o8_prev_last_frame_is_the_previous_live_scene_and_never_the_scene_itself(client):
    p = _project(client)
    a, b, c, d = _put(client, p["id"], 1, [_scene(action_text=ACTION + f" {n}")
                                           for n in "ABCD"]).json()["scenes"]
    _put(client, p["id"], 2, [_keep(a), _keep(c), _keep(d)], _gone(b))
    r = client.patch(f"/api/studio/scenes/{d['id']}", json={
        "expected_edit_version": 0, "source_frame": {"kind": "prev_last_frame"}})
    assert r.json()["scene"]["source_frame"] == {"kind": "prev_last_frame", "scene_id": c["id"],
                                                 "asset_id": None, "auto": True}
    me = client.patch(f"/api/studio/scenes/{c['id']}", json={
        "expected_edit_version": 0, "source_frame": {"kind": "prev_last_frame",
                                                     "scene_id": c["id"]}})
    assert (me.status_code, me.json()["error"]["code"], me.json()["error"]["detail"]) == (
        400, "source_scene_unknown", {"scene_id": c["id"]})
    first = client.patch(f"/api/studio/scenes/{a['id']}", json={
        "expected_edit_version": 0, "source_frame": {"kind": "prev_last_frame"}})
    assert first.json()["scene"]["source_frame"] == {
        "kind": "prev_last_frame", "scene_id": None, "asset_id": None, "auto": True}


def test_lint_gets_the_scenes_cast_tags_from_their_refs(client):
    """L6 only looks at scenes with the hero on screen: with refs it is a finding on scene 2,
    with the same texts and no refs there is none (review r1, 6)."""
    p = _project(client)
    _with_cast(client, p["id"])                       # anna: «a woman with a braid»
    braid = ACTION.replace("A woman", "A woman with a braid")
    hat = ACTION.replace("A woman", "A woman with a red hat")
    chain = lambda refs: [_scene(action_text=braid, refs=refs),            # noqa: E731
                          _scene(action_text=hat, refs=refs,
                                 source_frame={"kind": "prev_last_frame"})]
    one, two = _put(client, p["id"], 2, chain(["anna"])).json()["scenes"]
    found = client.post(f"/api/studio/projects/{p['id']}/lint", json={}).json()["findings"]
    assert found[one["id"]] == []
    assert [(f["rule"], f["severity"], f["field"]) for f in found[two["id"]]] == [
        ("L6", "warning", "action_text")]
    assert found[two["id"]][0]["message"] == (
        "Описание @anna расходится с другими сценами цепочки: вставьте дословно "
        "«a woman with a braid»")
    bare = _put(client, p["id"], 3, [_keep(one, refs=[]), _keep(two, refs=[], source_frame={
        "kind": "prev_last_frame"})]).json()
    assert bare["lint"] == {one["id"]: [], two["id"]: []}
    again = client.post(f"/api/studio/projects/{p['id']}/lint", json={}).json()["findings"]
    assert again == {one["id"]: [], two["id"]: []}


def test_the_409_of_o7_carries_the_whole_current_scene(client):
    p = _project(client)
    s, t = _put(client, p["id"], 1, [_scene(), _scene()]).json()["scenes"]
    client.patch(f"/api/studio/scenes/{s['id']}", json={"expected_edit_version": 0, "seed": 5})
    now = client.get(f"/api/studio/projects/{p['id']}").json()["scenes"]
    kept = _put(client, p["id"], 2, [_keep(s), _keep(t)])        # s read at version 0
    assert (kept.status_code, kept.json()["error"]["detail"]) == (
        409, {"scene_id": s["id"], "current": now[0]})
    silent = _put(client, p["id"], 2, [_keep(t)])               # s in neither list
    assert (silent.status_code, silent.json()["error"]["detail"]) == (
        409, {"scene_id": s["id"], "current": now[0]})
    deleting = _put(client, p["id"], 2, [_keep(t)], _gone(s))
    assert (deleting.status_code, deleting.json()["error"]["detail"]) == (
        409, {"scene_id": s["id"], "current": now[0]})


def test_duplicate_deletions_and_a_version_on_a_new_scene_are_400(client):
    p = _project(client)
    s, t = _put(client, p["id"], 1, [_scene(), _scene()]).json()["scenes"]
    dup = _put(client, p["id"], 2, [_keep(t)], _gone(s) + _gone(s))
    assert (dup.status_code, dup.json()["error"]["code"], dup.json()["error"]["detail"]) == (
        400, "args_invalid", {"scene_ids": [s["id"], s["id"]]})
    fresh = _put(client, p["id"], 2, [_keep(s), _keep(t), _scene(expected_edit_version=0)])
    assert (fresh.status_code, fresh.json()["error"]["code"]) == (400, "args_invalid")
    assert [x["id"] for x in client.get(f"/api/studio/projects/{p['id']}").json()["scenes"]] \
        == [s["id"], t["id"]]


# --- the frame source derived from the order is remembered as derived (coordinator ruling) ----

def _src(scene):
    return (scene["source_frame"]["scene_id"], scene["source_frame"]["auto"])


def test_an_auto_source_follows_the_order_and_an_explicit_one_stays_put(client):
    p = _project(client)
    a, b, c = _put(client, p["id"], 1, [
        _scene(), _scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " B"),
        _scene(source_frame={"kind": "prev_last_frame", "scene_id": None},
               action_text=ACTION + " C")]).json()["scenes"]
    assert [_src(x) for x in (a, b, c)] == [(None, False), (a["id"], True), (b["id"], True)]
    # the master sends the auto source back WITHOUT a scene_id; the order is now a, c, b
    r = _put(client, p["id"], 2, [_keep(a), _keep(c, source_frame={"kind": "prev_last_frame"}),
                                  _keep(b, source_frame={"kind": "prev_last_frame"})]).json()
    assert [(x["id"], *_src(x)) for x in r["scenes"]] == [
        (a["id"], None, False), (c["id"], a["id"], True), (b["id"], c["id"], True)]
    # an explicit source names its scene: it does not move with the order and is not auto
    a2, c2, b2 = r["scenes"]
    auto = {"source_frame": {"kind": "prev_last_frame"}}
    e = _put(client, p["id"], 3, [_keep(a2), _keep(c2, **auto), _keep(b2, source_frame={
        "kind": "prev_last_frame", "scene_id": a["id"]})]).json()["scenes"]
    assert [_src(x) for x in e] == [(None, False), (a["id"], True), (a["id"], False)]
    # reorder to a, b, c: b keeps naming a (explicit); c, derived, now follows b
    again = _put(client, p["id"], 4, [_keep(e[0]), _keep(e[2], source_frame={
        "kind": "prev_last_frame", "scene_id": a["id"]}), _keep(e[1], source_frame={
            "kind": "prev_last_frame"})]).json()["scenes"]
    assert [(x["id"], *_src(x)) for x in again] == [
        (a["id"], None, False), (b["id"], a["id"], False), (c["id"], b["id"], True)]
    stored = client.get(f"/api/studio/projects/{p['id']}").json()["scenes"]
    assert stored == again                       # the flag is stored, not recomputed on read


def test_deleting_the_middle_scene_moves_an_auto_source_back_and_refuses_an_explicit_one(client):
    p = _project(client)
    a, b, c = _put(client, p["id"], 1, [
        _scene(), _scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " B"),
        _scene(source_frame={"kind": "prev_last_frame"}, action_text=ACTION + " C")]).json()["scenes"]
    auto = _put(client, p["id"], 2, [_keep(a), _keep(c, source_frame={
        "kind": "prev_last_frame"})], _gone(b))
    assert auto.status_code == 200
    (_, moved) = auto.json()["scenes"]
    assert (moved["id"], moved["revision"], _src(moved)) == (c["id"], 2, (a["id"], True))
    explicit = _put(client, p["id"], 3, [_keep(a), _keep(moved, source_frame={
        "kind": "prev_last_frame", "scene_id": a["id"]})])
    assert explicit.status_code == 200
    x, y = explicit.json()["scenes"]
    assert _src(y) == (a["id"], False)
    # a second middle scene to delete, the next one pointing at it explicitly
    z = _put(client, p["id"], 4, [_keep(x), _keep(y), _scene(source_frame={
        "kind": "prev_last_frame"})]).json()["scenes"]
    cut = _put(client, p["id"], 5, [_keep(z[0]), _keep(z[2], source_frame={
        "kind": "prev_last_frame", "scene_id": z[1]["id"]})], _gone(z[1]))
    assert (cut.status_code, cut.json()["error"]["code"], cut.json()["error"]["detail"]) == (
        400, "source_scene_unknown", {"scene_id": z[1]["id"]})


def test_editing_another_scene_leaves_an_auto_source_alone(client):
    p = _project(client)
    a, b = _put(client, p["id"], 1, [_scene(), _scene(source_frame={
        "kind": "prev_last_frame"})]).json()["scenes"]
    client.patch(f"/api/studio/scenes/{a['id']}", json={"expected_edit_version": 0, "seed": 9})
    (_, kept) = client.get(f"/api/studio/projects/{p['id']}").json()["scenes"]
    assert (kept["revision"], kept["edit_version"], _src(kept)) == (1, 0, (a["id"], True))
    pinned = client.patch(f"/api/studio/scenes/{b['id']}", json={
        "expected_edit_version": 0, "source_frame": {"kind": "prev_last_frame",
                                                     "scene_id": a["id"]}}).json()["scene"]
    assert (pinned["revision"], pinned["edit_version"], _src(pinned)) == (1, 1, (a["id"], False))
