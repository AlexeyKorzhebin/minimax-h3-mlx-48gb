"""Validation and estimates through the HTTP API when H3_ENGINE=sglang (spec §3.3.4, §3.3.15)."""
import json

import pytest

from h3_48gb.engines import estimate as est
from h3_48gb.engines import sglang_args as sa
from test_web import _call, _pending, queue_server  # noqa: F401  (fixture)


def test_estimate_prefers_the_median_of_the_last_ten_matching_runs(tmp_path):
    for wall in (100, 900, 200, 300):
        est.record(tmp_path, width=896, height=512, frames=175, wall_s=wall)
    est.record(tmp_path, width=896, height=512, frames=124, wall_s=5000)
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=175) == \
        {"seconds": 250.0, "source": "history", "samples": 4}


def test_estimate_uses_only_the_last_ten_runs(tmp_path):
    for wall in range(1, 13):
        est.record(tmp_path, width=896, height=512, frames=175, wall_s=wall)
    # last ten are 3..12 -> median 7.5; the first ten (1..10) would give 5.5
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=175) == \
        {"seconds": 7.5, "source": "history", "samples": 10}


def test_estimate_falls_back_to_the_bench_table_by_nearest_frames(tmp_path):
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=175) == \
        {"seconds": 2810.0, "source": "table", "samples": 0}
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=130) == \
        {"seconds": 345.0, "source": "table", "samples": 0}
    assert est.estimate_seconds(tmp_path, width=768, height=768, frames=209) == \
        {"seconds": 2820.0, "source": "table", "samples": 0}


def test_estimate_skips_corrupt_history_lines(tmp_path):
    (tmp_path / est.HISTORY_NAME).write_text("not json\n" + json.dumps(
        {"width": 896, "height": 512, "frames": 175, "wall_s": 42}) + "\n", encoding="utf-8")
    assert est.estimate_seconds(tmp_path, width=896, height=512, frames=175)["seconds"] == 42.0


def _sglang_job_args(live, *extra):
    """Every sglang scene carries at least one reference (spec §4.1.3)."""
    ref = live.outdir / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    return ["generate", "кот", "--width", "896", "--height", "512",
            "--duration", str(175 / 24), "--tag", "ночь", "--outdir", str(live.outdir),
            "--ref", str(ref), *extra]


def test_a_sglang_job_is_queued_without_a_dry_run_subprocess(queue_server, monkeypatch):  # noqa: F811
    from h3_48gb import web

    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setattr(web.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no subprocess on the sglang path")))
    status, body = _call(queue_server, "POST", "/api/jobs",
                         {"args": _sglang_job_args(queue_server), "note": ""})
    assert status == 200, body
    job = body["job"]
    # queue.submit relocates --outdir into a per-job subdirectory; the stem the queue stores must
    # be exactly the one the adapter will derive from the relocated argv.
    assert sa.output_stem(sa.parse(job["args"], check_files=False)) == job["output_stem"]
    assert body["estimate"] == {"seconds": 2810.0, "source": "table", "samples": 0}


def test_mlx_flags_are_refused_on_sglang(queue_server, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    status, body = _call(queue_server, "POST", "/api/jobs",
                         {"args": _sglang_job_args(queue_server, "--turbo-strength", "0.45"),
                          "note": ""})
    assert (status, body["error"]["code"], body["error"]["message"]) == \
        (400, "unsupported_on_sglang", "unsupported_on_sglang: --turbo-strength")
    assert _pending(queue_server) == []


def test_a_ref_outside_the_roots_is_refused(queue_server, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    status, body = _call(queue_server, "POST", "/api/jobs",
                         {"args": _sglang_job_args(queue_server, "--ref", "/etc/passwd"), "note": ""})
    assert (status, body["error"]["code"]) == (400, "path_outside_root")


def test_estimate_route_on_sglang(queue_server, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    status, body = _call(queue_server, "POST", "/api/estimate",
                         {"args": _sglang_job_args(queue_server)})
    assert (status, body) == (200, {"ok": True, "estimate":
                                    {"seconds": 2810.0, "source": "table", "samples": 0}})
