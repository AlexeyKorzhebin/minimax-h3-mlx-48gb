"""`python -m h3_48gb.studio worker|setting` (spec §4.2, §6 SETTING): one worker per outdir, the
settings it is built from."""
from __future__ import annotations

import fcntl

import pytest

from h3_48gb.studio import config, db, settings
from h3_48gb.studio.__main__ import main
from h3_48gb.studio.gpu import DispatcherGate, LocalGate
from h3_48gb.studio.worker import build_worker


def test_setting_shows_and_sets(tmp_path, capsys):
    out = str(tmp_path / "out")
    assert main(["setting", "--outdir", out, "comfy.url", "http://127.0.0.1:8188"]) == 0
    assert capsys.readouterr().out == "comfy.url = http://127.0.0.1:8188\n"
    assert main(["setting", "--outdir", out]) == 0
    assert capsys.readouterr().out == (
        "comfy.url = http://127.0.0.1:8188\n"
        "comfy.output_dir = /home/alex/Outputs/comfy-h3\n"
        "comfy.scope = dispatcher:comfy-studio\n"
        "dispatcher.url = http://127.0.0.1:8790\n"
        "machine = alex-neuro\n")
    assert main(["setting", "--outdir", out, "nope"]) == 2


def test_a_second_worker_is_refused(tmp_path, capsys):
    home = config.studio_dir(tmp_path / "out")
    home.mkdir(parents=True)
    with open(home / "worker.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main(["worker", "--outdir", str(tmp_path / "out")]) == 2
    assert capsys.readouterr().err == "studio: второй воркер не нужен — worker.lock занят\n"


def test_the_worker_is_built_from_the_settings(tmp_path):
    outdir = tmp_path / "out"
    worker = build_worker(outdir)
    assert isinstance(worker.gate, DispatcherGate) and worker.gate.engine == "comfy-studio"
    assert (worker.executor.url, worker.executor.scope, sorted(worker.kinds)) == (
        "http://127.0.0.1:8189", "dispatcher:comfy-studio", ["image"])
    conn = db.connect(config.studio_dir(outdir) / "studio.sqlite")
    settings.put(conn, "comfy.scope", "local-mac")
    worker = build_worker(outdir)
    assert isinstance(worker.gate, LocalGate) and worker.idle is None
    assert worker.gate.lock_path == config.studio_dir(outdir) / "gpu-mac.lock"
    settings.put(conn, "comfy.scope", "somewhere")
    with pytest.raises(ValueError, match="dispatcher:<engine> или local-mac"):
        build_worker(outdir)
