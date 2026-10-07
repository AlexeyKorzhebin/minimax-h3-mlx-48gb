"""H3_ENGINE (spec §3.3.1) and the Mac-only behaviour it gates (§3.3.7)."""
import threading

import pytest

from h3_48gb import engine
from h3_48gb import queue as q
from h3_48gb import worker
from test_web import _call, server  # noqa: F401  (fixture)
from test_worker import _stop_after


def test_engine_defaults_to_mlx_and_normalises_case():
    assert engine.current({}) == "mlx"
    assert engine.current({"H3_ENGINE": ""}) == "mlx"
    assert engine.current({"H3_ENGINE": " SGLang "}) == "sglang"
    assert engine.is_sglang({"H3_ENGINE": "sglang"}) is True
    assert engine.is_sglang({}) is False


def test_an_unknown_engine_is_refused():
    with pytest.raises(engine.UnknownEngine):
        engine.current({"H3_ENGINE": "cuda"})


class _Job:
    args = ["generate", "кот", "--tag", "a"]


def test_job_command_wraps_caffeinate_only_on_darwin():
    assert worker.job_command(_Job(), python="py", platform="darwin") == \
        ["caffeinate", "-dimsu", "py", "-m", "h3_48gb", "generate", "кот", "--tag", "a"]
    assert worker.job_command(_Job(), python="py", platform="linux") == \
        ["py", "-m", "h3_48gb", "generate", "кот", "--tag", "a"]


def test_caffeinate_block_spawns_nothing_off_darwin():
    spawned = []

    def spawn(cmd, **kw):
        spawned.append(cmd)
        raise AssertionError("caffeinate must not be started on linux")

    with worker._caffeinate_block(spawn, platform="linux"):
        pass
    assert spawned == []


def _count_llm_gate_calls(monkeypatch, tmp_path, engine_name):
    calls = []
    monkeypatch.setattr(worker, "_llm_holds_gpu", lambda outdir: calls.append(outdir) or False)
    if engine_name:
        monkeypatch.setenv("H3_ENGINE", engine_name)
    root = q.layout(tmp_path / "queue")["root"]
    q.set_paused(root, False)
    worker.main_loop(root, poll=0.01, stop=_stop_after(0.3))
    return calls


def test_the_llm_gate_is_consulted_on_mlx(monkeypatch, tmp_path):
    assert len(_count_llm_gate_calls(monkeypatch, tmp_path, None)) >= 1


def test_the_llm_gate_is_never_consulted_on_sglang(monkeypatch, tmp_path):
    assert _count_llm_gate_calls(monkeypatch, tmp_path, "sglang") == []


def test_state_reports_engine_and_platform(server, monkeypatch):  # noqa: F811
    from h3_48gb import web

    monkeypatch.setattr(web, "_PLATFORM", "linux")
    monkeypatch.setenv("H3_ENGINE", "sglang")
    status, body = _call(server, "GET", "/api/state")
    assert status == 200
    assert (body["engine"], body["platform"]) == ("sglang", "linux")


def test_reveal_is_refused_off_darwin(server, monkeypatch):  # noqa: F811
    from h3_48gb import web

    monkeypatch.setattr(web, "_PLATFORM", "linux")
    status, body = _call(server, "POST", "/api/jobs/anything/reveal", {})
    assert status == 409
    assert body["error"]["code"] == "reveal_unsupported"


def test_worker_refuses_an_unknown_engine(monkeypatch, tmp_path):
    from h3_48gb import cli

    monkeypatch.setenv("H3_ENGINE", "cuda")
    with pytest.raises(cli.CliError) as excinfo:
        cli.run_worker(tmp_path, 5.0)
    assert excinfo.value.code == "engine_unknown"
