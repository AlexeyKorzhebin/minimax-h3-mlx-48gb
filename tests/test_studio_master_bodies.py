"""The O7 bodies the master builds (task 12, `toSceneIn` / `afterDelete`) against the real server:
an operation on the set leaves every scene it is not about exactly as saved (review r8)."""
from __future__ import annotations

import pytest
from _studio_fixtures import ScriptedLlm, make_client

from h3_48gb.studio import ops

SCENE_FIELDS = ["mode", "action_text", "performance_text", "sound_text", "music_text",
                "state_in_text", "state_out_text", "requested_s", "seed", "source_frame", "refs"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    seeds = iter(range(101, 200))
    monkeypatch.setattr(ops, "_new_seed", lambda: next(seeds))
    return make_client(tmp_path, llm=ScriptedLlm([]))


def _source(scene: dict) -> dict:
    """The source as `toSceneIn` sends it: one derived from the order (`auto`) goes WITHOUT a
    scene_id — the server derives it again; an explicit one goes with it. The server answers
    `auto` but forbids it on input (400)."""
    src = scene["source_frame"]
    if src["auto"]:
        return {"kind": "prev_last_frame"}
    return {"kind": src["kind"], "scene_id": src["scene_id"], "asset_id": src["asset_id"]}


def _copy(scene: dict) -> dict:
    """What the master's `toSceneIn` sends: every field as stored."""
    return {"scene_id": scene["id"], "expected_edit_version": scene["edit_version"],
            **{f: scene[f] for f in SCENE_FIELDS if f != "source_frame"},
            "source_frame": _source(scene)}


def _three(client):
    """S1, S2, S3 continuing S2 — then the owner points S3's frame at S1 explicitly (O8)."""
    p = client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"}).json()
    base = {"mode": "from_frame", "requested_s": 8.0, "refs": []}
    s1, s2, s3 = client.put(f"/api/studio/projects/{p['id']}/scenes", json={
        "expected_revision": 1, "deleted": [], "scenes": [
            {**base, "source_frame": {"kind": "none"}},
            {**base, "source_frame": {"kind": "prev_last_frame"}},
            {**base, "source_frame": {"kind": "prev_last_frame"}}]}).json()["scenes"]
    s3 = client.patch(f"/api/studio/scenes/{s3['id']}", json={
        "expected_edit_version": 0,
        "source_frame": {"kind": "prev_last_frame", "scene_id": s1["id"]}}).json()["scene"]
    return p["id"], s1, s2, s3


def _scenes(client, pid):
    return client.get(f"/api/studio/projects/{pid}").json()["scenes"]


def test_adding_a_scene_keeps_s3_on_s1_and_every_saved_scene_untouched(client):
    pid, s1, s2, s3 = _three(client)
    before = _scenes(client, pid)
    r = client.put(f"/api/studio/projects/{pid}/scenes", json={
        "expected_revision": 2, "deleted": [],
        "scenes": [_copy(s) for s in before] + [
            {"mode": "from_frame", "requested_s": 8.0, "source_frame": {"kind": "prev_last_frame"},
             "refs": []}]})
    assert r.status_code == 200
    assert r.json()["diff"]["unchanged"] == [s1["id"], s2["id"], s3["id"]]
    after = _scenes(client, pid)
    assert after[:3] == before                                  # every field, versions included
    assert after[2]["source_frame"] == {"kind": "prev_last_frame", "scene_id": s1["id"],
                                        "asset_id": None, "auto": False}


def test_deleting_the_middle_scene_relinks_only_its_continuation_explicitly(client):
    pid, s1, s2, s3 = _three(client)                            # S3 → S1, so S2 has no continuation
    before = _scenes(client, pid)
    r = client.put(f"/api/studio/projects/{pid}/scenes", json={
        "expected_revision": 2, "scenes": [_copy(before[0]), _copy(before[2])],
        "deleted": [{"scene_id": s2["id"], "expected_edit_version": before[1]["edit_version"]}]})
    assert r.status_code == 200
    after = _scenes(client, pid)
    assert [{k: v for k, v in s.items() if k != "idx"} for s in after] == [
        {k: v for k, v in s.items() if k != "idx"} for s in (before[0], before[2])]


def test_deleting_the_middle_of_an_auto_chain_keeps_the_continuation_derived(client):
    pid = client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"}).json()["id"]
    base = {"mode": "from_frame", "requested_s": 8.0, "refs": []}
    s1, s2, s3 = client.put(f"/api/studio/projects/{pid}/scenes", json={
        "expected_revision": 1, "deleted": [], "scenes": [
            {**base, "source_frame": {"kind": "none"}},
            {**base, "source_frame": {"kind": "prev_last_frame"}},
            {**base, "source_frame": {"kind": "prev_last_frame"}}]}).json()["scenes"]
    before = _scenes(client, pid)
    assert [s["source_frame"]["auto"] for s in before] == [False, True, True]
    r = client.put(f"/api/studio/projects/{pid}/scenes", json={
        "expected_revision": 2, "scenes": [_copy(before[0]), _copy(before[2])],
        "deleted": [{"scene_id": s2["id"], "expected_edit_version": before[1]["edit_version"]}]})
    assert r.status_code == 200
    after = _scenes(client, pid)
    assert after[1]["source_frame"] == {"kind": "prev_last_frame", "scene_id": s1["id"],
                                        "asset_id": None, "auto": True}
