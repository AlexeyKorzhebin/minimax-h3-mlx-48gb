"""Gates on sglang: durations snapped to the delivered grid, @tags checked before a single scene
is queued (spec §3.3.5, §3.5, §4.1.5), and the chat seeing the library tags."""
import sys

import pytest

from h3_48gb import assemble
from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from h3_48gb.engines import sglang_args as sa
from test_web import _call, _pending, _serve


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    (outdir / "uploads" / "face.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    lib.create_card(outdir, tag="@alice", kind="person", description="a young woman",
                    assets=[outdir / "uploads" / "face.png"])
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _video(live, prompts, durations, fresh=()):
    proj = p.create_project(live.outdir, "video", "Gate")
    proj.scenes = [{"idx": i, "prompt": text, "duration": d, "status": "pending", "job_id": None,
                    "clip_path": None, "keyframe_path": None, "fresh_start": i in fresh}
                   for i, (text, d) in enumerate(zip(prompts, durations))]
    proj.stages["script"] = "awaiting_approval"
    proj.save()
    proj.set_references([{"tag": "@alice", "version": 1}])
    return proj


def test_approving_the_script_snaps_durations_to_the_delivered_grid(live):
    proj = _video(live, ["@alice sits", "@alice waves", "@alice runs"], [7.0, 7.0, 10.0], fresh=(2,))
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    assert [s["duration"] for s in p.load_project(proj.path).scenes] == \
        [175 / 24, 174 / 24, 243 / 24]
    (job,) = _pending(live)
    assert sa.parse(job.args, check_files=False).frames == 175


def test_every_scene_without_a_tag_is_refused_and_nothing_is_queued(live):
    """spec §4.1.3: every scene needs a reference -- the first and a fresh_start one too, not
    only a chained one."""
    proj = _video(live, ["a cat", "@alice and a dog", "a bird"], [7.0, 7.0, 7.0], fresh=(2,))
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 400
    assert body["error"]["code"] == "scene_references_invalid"
    assert body["error"]["detail"] == {"scenes": [
        {"idx": 0, "code": "ref2va_needs_reference",
         "message": "нужен хотя бы один референс (@тег) в сцене"},
        {"idx": 2, "code": "ref2va_needs_reference",
         "message": "нужен хотя бы один референс (@тег) в сцене"}]}
    reloaded = p.load_project(proj.path)
    assert reloaded.stages["script"] == "awaiting_approval"
    assert [s["duration"] for s in reloaded.scenes] == [7.0, 7.0, 7.0]
    assert _pending(live) == []


def test_a_refusal_at_approval_leaves_scenes_editable_again(live, monkeypatch):
    """Final re-review: `advance_project` sets stages.scenes `running` before it builds the first
    scene's arguments; a refusal there rolled the scene and the durations back but left the stage
    `running`, so `PUT /scenes` (which wants `draft`) refused the fix."""
    real = assemble._scene_generate_args_sglang

    def refuse(*args, **kwargs):          # the pre-check calls it too; only the claimed submit fails
        if sys._getframe(1).f_code.co_name == "_submit_next_scene_sglang":
            raise sa.SglangArgsError("sglang_args_invalid", "refused after the claim")
        return real(*args, **kwargs)
    monkeypatch.setattr(assemble, "_scene_generate_args_sglang", refuse)
    proj = _video(live, ["@alice sits"], [7.0])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert (status, body["error"]["code"]) == (400, "sglang_args_invalid")
    reloaded = p.load_project(proj.path)
    assert reloaded.stages["scenes"] == "draft"
    assert [(s["status"], s["duration"]) for s in reloaded.scenes] == [("pending", 7.0)]
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/scenes",
                         {"scenes": [{"prompt": "@alice waves", "duration": 7}]})
    assert status == 200, body


def test_settings_route_changes_the_i2v_prefix(live):
    proj = _video(live, ["@alice"], [7.0])
    assert p.load_project(proj.path).i2v_prefix == p.DEFAULT_I2V_PREFIX
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/settings",
                         {"i2v_prefix": "Continue the shot."})
    assert status == 200, body
    assert (body["project"]["i2v_prefix"], p.load_project(proj.path).i2v_prefix) == \
        ("Continue the shot.", "Continue the shot.")


def test_an_unknown_and_a_malformed_tag_are_named_per_scene(live):
    proj = _video(live, ["@bob in a room", "@Alice waves"], [7.0, 7.0])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert body["error"]["detail"] == {"scenes": [
        {"idx": 0, "code": "unknown_tag", "message": "теги не подключены к проекту: @bob"},
        {"idx": 1, "code": "tag_invalid",
         "message": "тег @Alice: пишется строчными, 2–32 символа из a-z, 0-9, -"}]}


def test_too_many_pictures_is_refused(live, monkeypatch):
    monkeypatch.setenv("H3_MAX_REF_IMAGES", "0")
    proj = _video(live, ["@alice"], [7.0])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert body["error"]["detail"] == {"scenes": [
        {"idx": 0, "code": "sglang_args_invalid", "message": "H3_MAX_REF_IMAGES должен быть ≥ 1"}]}


def test_on_mlx_nothing_is_snapped_or_checked(live, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "mlx")
    proj = _video(live, ["a cat", "a dog"], [7.0, 7.0])   # no tags: fine on mlx
    monkeypatch.setattr("h3_48gb.assemble.advance_project", lambda *a, **k: {"action": "stub"})
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    assert [s["duration"] for s in p.load_project(proj.path).scenes] == [7.0, 7.0]


def test_the_picture_limit_counts_card_assets(live, monkeypatch):
    monkeypatch.setenv("H3_MAX_REF_IMAGES", "1")
    (live.outdir / "uploads" / "back.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    lib.update_card(live.outdir, "@alice", assets=[live.outdir / "uploads" / "face.png",
                                                    live.outdir / "uploads" / "back.png"])
    proj = _video(live, ["@alice"], [7.0])
    proj.set_references([{"tag": "@alice", "version": 2}])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert body["error"]["detail"] == {"scenes": [
        {"idx": 0, "code": "too_many_reference_images",
         "message": "картинок-референсов 2, а можно не больше 1"}]}


def _durations(proj):
    return [s["duration"] for s in p.load_project(proj.path).scenes]


def test_the_ends_of_the_range_snap_inside_what_sglang_accepts(live):
    """15.0 s -> 345 frames (not 362); a chained 2.5 s -> delivers 72 frames (requests 73)."""
    proj = _video(live, ["@alice sits", "@alice waves"], [15.0, 2.5])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    assert _durations(proj) == [345 / 24, 72 / 24]
    (job,) = _pending(live)
    assert sa.parse(job.args, check_files=False).frames == 345


def test_a_first_scene_of_2_5_seconds_snaps_to_73_frames(live):
    proj = _video(live, ["@alice sits"], [2.5])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    assert _durations(proj) == [73 / 24]


def test_a_refusal_at_the_gate_leaves_the_durations_on_disk_untouched(live):
    proj = _video(live, ["@alice sits", "@bob waves"], [7.0, 7.0])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 400
    assert body["error"]["detail"] == {"scenes": [
        {"idx": 1, "code": "unknown_tag", "message": "теги не подключены к проекту: @bob"}]}
    assert _durations(proj) == [7.0, 7.0]
    assert _pending(live) == []


def test_an_error_from_advance_project_is_400_and_restores_the_durations(live, monkeypatch):
    proj = _video(live, ["@alice sits"], [7.0])

    def boom(*a, **k):
        raise sa.SglangArgsError("sglang_args_invalid", "boom", {})
    monkeypatch.setattr("h3_48gb.assemble.advance_project", boom)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 400
    assert (body["error"]["code"], body["error"]["message"]) == ("sglang_args_invalid", "boom")
    assert _durations(proj) == [7.0]
    assert p.load_project(proj.path).stages["script"] == "awaiting_approval"
