"""UI-gap API 2: a scene's explicit `refs: ["@tag", ...]` connect cards as reference conditions
without a mention in the text and without a `<Subject N>` substitution. Condition order:
keyframe, refs in order, then the tags the text mentions, no duplicates."""
from pathlib import Path

import pytest

from h3_48gb import assemble
from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from h3_48gb.engines import sglang as sglang_engine
from h3_48gb.engines import sglang_args as sa
from test_web import _call, _pending, _serve

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    for name in ("face.png", "back.png", "opening.png"):
        (outdir / "uploads" / name).write_bytes(PNG)
    lib.create_card(outdir, tag="@amazon", kind="person", description="an armored amazon",
                    assets=[outdir / "uploads" / "face.png", outdir / "uploads" / "back.png"])
    lib.create_card(outdir, tag="@arena", kind="environment", description="a sand arena",
                    assets=[outdir / "uploads" / "opening.png"])
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _error(answer):
    error = answer.get("error") or {}
    return error.get("code"), error.get("message")


def _payload_of_first_scene(live, scene):
    proj = p.create_project(live.outdir, "video", "Refs")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes",
                         {"scenes": [scene], "references": [{"tag": "@amazon"}, {"tag": "@arena"}]})
    assert status == 200, body
    assert p.load_project(proj.path).scenes[0].get("refs") == scene.get("refs")
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    (job,) = _pending(live)
    return sglang_engine.build_payload(sa.parse(job.args, check_files=False))


def _uri(live, name):
    return "file://" + str(live.outdir / "library" / name)


def test_refs_come_after_the_keyframe_and_before_the_tags_of_the_text(live):
    payload = _payload_of_first_scene(live, {
        "prompt": "@amazon fights", "duration": 8, "start_image": "@amazon", "refs": ["@arena"]})
    assert payload["prompt"] == (
        "subject_definitions:\n"
        "<Subject 1> is an armored amazon, appearance from <Picture 2>, <Picture 3>.\n\n"
        "<Subject 1> fights")
    assert [(c["role"], Path(c["uri"].removeprefix("file://")).name,
             c.get("frame_index")) for c in payload["conditions"]] == [
        ("keyframe", "01-face.png", 0), ("reference", "01-opening.png", None),
        ("reference", "01-face.png", None), ("reference", "02-back.png", None)]


def test_refs_alone_connect_cards_with_no_subject_block_and_no_substitution(live):
    payload = _payload_of_first_scene(live, {
        "prompt": "a woman fights, no tags at all", "duration": 8, "refs": ["@amazon", "@arena"]})
    assert payload["prompt"] == "a woman fights, no tags at all"
    assert [Path(c["uri"]).name for c in payload["conditions"]] == [
        "01-face.png", "02-back.png", "01-opening.png"]
    assert {c["role"] for c in payload["conditions"]} == {"reference"}


def test_a_tag_in_refs_and_in_the_text_gives_its_pictures_once(live):
    payload = _payload_of_first_scene(live, {
        "prompt": "@arena then @amazon", "duration": 8, "refs": ["@amazon"]})
    assert payload["prompt"] == (
        "subject_definitions:\n"
        "<Subject 1> is a sand arena, appearance from <Picture 3>.\n"
        "<Subject 2> is an armored amazon, appearance from <Picture 1>, <Picture 2>.\n\n"
        "<Subject 1> then <Subject 2>")
    assert [Path(c["uri"]).name for c in payload["conditions"]] == [
        "01-face.png", "02-back.png", "01-opening.png"]


def test_a_chained_scene_numbers_pictures_from_the_references_not_the_keyframe(tmp_path):
    out = tmp_path / "outdir"
    (out / "uploads").mkdir(parents=True)
    for name in ("a.png", "b.png", "k.png"):
        (out / "uploads" / name).write_bytes(PNG)
    lib.create_card(out, tag="@amazon", kind="person", description="an amazon",
                    assets=[out / "uploads" / "a.png"])
    lib.create_card(out, tag="@arena", kind="environment", description="an arena",
                    assets=[out / "uploads" / "b.png"])
    refs = [{"tag": "@amazon", "version": 1}, {"tag": "@arena", "version": 1}]
    ref2va = lib.build_ref2va("@arena at dusk", refs, out, extra_refs=["@amazon"])
    args, _ = assemble._scene_generate_args_sglang(
        {"idx": 1, "prompt": "@arena at dusk", "duration": 174 / 24},
        keyframe=out / "uploads" / "k.png", chained=True, ref2va=ref2va, track_piece=None,
        scenes_dir=tmp_path)
    payload = sglang_engine.build_payload(sa.parse(args, check_files=False))
    assert payload["prompt"] == ("subject_definitions:\n<Subject 1> is an arena, appearance from "
                                 "<Picture 2>.\n\n<Subject 1> at dusk")
    assert [(c["role"], Path(c["uri"]).name) for c in payload["conditions"]] == [
        ("keyframe", "k.png"), ("reference", "01-a.png"), ("reference", "01-b.png")]


@pytest.mark.parametrize("refs,code,message", [
    ("@amazon", "args_invalid", "`scenes[0].refs` must be a list of @tags"),
    ([7], "args_invalid", "`scenes[0].refs` must be a list of @tags"),
    (["@Bad"], "args_invalid", "`scenes[0].refs`: @Bad is not a tag"),
    (["@beach"], "unknown_tag", "refs @beach: тег не подключён к проекту"),
])
def test_bad_refs_are_refused_at_load(live, refs, code, message):
    proj = p.create_project(live.outdir, "video", "Refs")
    status, answer = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", {
        "scenes": [{"prompt": "x", "duration": 8, "refs": refs}],
        "references": [{"tag": "@amazon"}]})
    assert (status, *_error(answer)) == (400, code, message)
    assert p.load_project(proj.path).scenes == []


def test_a_scene_with_neither_text_tag_nor_refs_is_still_refused_and_one_with_refs_passes(live):
    proj = p.create_project(live.outdir, "video", "Refs")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes", {
        "scenes": [{"prompt": "no tag", "duration": 8}, {"prompt": "no tag", "duration": 8,
                                                          "refs": ["@amazon"]}],
        "references": [{"tag": "@amazon"}]})
    assert status == 200, body
    status, answer = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert (status, answer["error"]["detail"]) == (400, {"scenes": [
        {"idx": 0, "code": "ref2va_needs_reference",
         "message": "нужен хотя бы один референс (@тег) в сцене"}]})
