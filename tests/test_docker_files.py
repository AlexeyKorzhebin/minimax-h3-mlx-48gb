"""The panel image (spec §3.2): what goes in, what stays out, and the entrypoint's one promise --
the container exits when either process dies."""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lines(name):
    return (ROOT / name).read_text(encoding="utf-8").splitlines()


def test_dockerfile_builds_the_panel_without_mlx():
    lines = _lines("Dockerfile")
    assert lines[0] == "FROM python:3.12-slim"
    assert "    && apt-get install -y --no-install-recommends ffmpeg nodejs tzdata \\" in lines
    assert "RUN pip install --no-cache-dir -r requirements-panel.txt" in lines
    assert "RUN pip install --no-cache-dir --no-deps -e ." in lines
    assert "USER 1000:1000" in lines
    assert 'ENTRYPOINT ["/app/docker/entrypoint.sh"]' in lines
    text = "\n".join(l for l in lines if not l.lstrip().startswith("#")).lower()
    assert "mlx" not in text and "cuda" not in text and "opencv" not in text


def test_compose_mounts_host_paths_at_the_same_paths_and_sets_the_spec_env():
    lines = [line.strip() for line in _lines("compose.yaml")]
    for expected in (
            "network_mode: host", 'user: "1000:1000"', "restart: unless-stopped",
            "TZ: Europe/Moscow", "HOME: /tmp", "init: true",
            "- /home/alex/Outputs/h3-panel:/home/alex/Outputs/h3-panel",
            "- /home/alex/Outputs/comfy/output:/home/alex/Outputs/comfy/output:ro",
            "- /home/alex/Outputs/comfy/output/h3panel:/home/alex/Outputs/comfy/output/h3panel",
            "- /home/alex/Projects/h3-bench/inputs:/home/alex/Projects/h3-bench/inputs:ro",
            "H3_ENGINE: sglang", "H3_OUTDIR: /home/alex/Outputs/h3-panel",
            'H3_ALLOWED_HOSTS: "192.168.100.50:8765,alex-neuro:8765"',
            "H3_SGLANG_URL: http://127.0.0.1:30020", "H3_COMFY_URL: http://127.0.0.1:8188",
            "H3_DISPATCHER_URL: http://127.0.0.1:8790",
            "H3_COMFY_OUTPUT_DIR: /home/alex/Outputs/comfy/output",
            'H3_IDLE_RELEASE_MIN: "15"', 'H3_MAX_REF_IMAGES: "5"'):
        assert expected in lines, expected


def test_compose_healthcheck_asks_the_page_itself_on_the_loopback():
    """`docker compose ps` says `healthy` only when the page answers /api/state: a live process
    with a dead page (or a page refusing its own Host) must read as unhealthy, not as `Up`."""
    lines = [line.strip() for line in _lines("compose.yaml")]
    assert "healthcheck:" in lines
    assert ('test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('
            "'http://127.0.0.1:8765/api/state', timeout=5)\"]") in lines


def test_dockerignore_keeps_the_context_small_but_keeps_what_the_panel_reads():
    ignored = set(_lines(".dockerignore"))
    # upstream/ is the MLX reference checkout: the package imports without it (task 2)
    assert {".git", "**/__pycache__", "*.egg-info", ".pytest_cache", "reference",
            "upstream", "logs"} <= ignored
    assert not ({"docs", "prompts", "tests", "h3_48gb"} & ignored)


def _stub_python(tmp_path, *, web_exit=None, worker_exit=None) -> Path:
    """`python -m h3_48gb web|worker`: the one given an exit code dies with it after a second,
    the other sleeps for 30 s (and so only ends if the entrypoint stops it)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python"

    def branch(exit_code):
        return "exec sleep 30" if exit_code is None else f"sleep 1; exit {exit_code}"

    stub.write_text("#!/bin/sh\n"
                    'echo "$@" >> "$STUB_LOG"\n'
                    'case "$3" in\n'
                    f"  web) {branch(web_exit)} ;;\n"
                    f"  worker) {branch(worker_exit)} ;;\n"
                    "esac\n")
    stub.chmod(0o755)
    return bin_dir


def _run_entrypoint(tmp_path, bin_dir):
    log = tmp_path / "calls.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log),
           "H3_OUTDIR": str(tmp_path / "out")}
    started = time.monotonic()
    result = subprocess.run([str(ROOT / "docker" / "entrypoint.sh")], env=env, timeout=20)
    assert time.monotonic() - started < 10, "the surviving stub (sleep 30) was not stopped"
    assert sorted(log.read_text().splitlines()) == sorted([
        f"-m h3_48gb web --host 0.0.0.0 --port 8765 --outdir {tmp_path / 'out'}",
        f"-m h3_48gb worker --outdir {tmp_path / 'out'}"])
    return result.returncode


def test_entrypoint_starts_both_and_exits_with_the_dead_workers_code(tmp_path):
    assert _run_entrypoint(tmp_path, _stub_python(tmp_path, worker_exit=3)) == 3


def test_entrypoint_exits_with_the_dead_webs_code(tmp_path):
    # the worker survives and is stopped by TERM (143): its status must not replace the web's
    assert _run_entrypoint(tmp_path, _stub_python(tmp_path, web_exit=5)) == 5


def test_entrypoint_refuses_to_start_without_an_outdir(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "H3_OUTDIR"}
    result = subprocess.run([str(ROOT / "docker" / "entrypoint.sh")], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "H3_OUTDIR must be set" in result.stderr
