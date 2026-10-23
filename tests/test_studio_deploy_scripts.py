"""Deploy helpers (plan task 5): the panel fingerprint must FAIL when a probe fails, and the
rollback tag must point at the image the running studio container uses (review r2, Н6, Н7).

docker, git and curl are replaced by stubs on PATH that answer from environment variables and
log their argv; nothing touches a real Docker daemon or the old panel."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "scripts" / "check_panel_untouched.sh"
PREV = ROOT / "scripts" / "studio_tag_prev.sh"

DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"
case "$1" in
  ps) [ -n "${STUB_PS:-}" ] && printf '%b\n' "$STUB_PS"; exit 0 ;;
  inspect) [ "${STUB_INSPECT_FAIL:-0}" = 1 ] && exit 1; printf '%s\n' "$STUB_IMAGE" ;;
  tag) exit 0 ;;
  *) exit 3 ;;
esac
"""
GIT = r"""#!/bin/sh
[ "${STUB_GIT_FAIL:-0}" = 1 ] && exit 128
printf '%s\n' "${STUB_SHA:-0123abc}"
"""
CURL = r"""#!/bin/sh
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { out=$2; shift; }; shift; done
[ "${STUB_CURL_EXIT:-0}" != 0 ] && exit "$STUB_CURL_EXIT"
printf '%s' "${STUB_BODY:-{\"ok\": true}}" > "$out"
printf '%s' "${STUB_HTTP:-200}"
"""


@pytest.fixture
def run(tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, text in (("docker", DOCKER), ("git", GIT), ("curl", CURL)):
        (stubs / name).write_text(text)
        (stubs / name).chmod(0o755)
    log = tmp_path / "docker.log"

    def _run(script: Path, *args: str, **env: str):
        full = {"PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(log),
                "STUB_IMAGE": "sha256:aaa", **env}
        done = subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                              env=full)
        calls = log.read_text().splitlines() if log.exists() else []
        return done, calls
    return _run


def test_a_healthy_panel_prints_its_fingerprint(run):
    done, _ = run(PANEL, STUB_PS="cid1")
    assert (done.returncode, done.stdout, done.stderr) == (
        0, "container cid1\nimage sha256:aaa\ncode 0123abc\nhttp 200\n", "")


@pytest.mark.parametrize("env, message", [
    ({"STUB_PS": "cid1", "STUB_HTTP": "500"},
     "panel check failed: GET http://127.0.0.1:8765/api/state answered 500\n"),
    ({"STUB_PS": "cid1", "STUB_BODY": "<html>"},
     "panel check failed: http://127.0.0.1:8765/api/state did not answer JSON\n"),
    ({"STUB_PS": "cid1", "STUB_CURL_EXIT": "7"},
     "panel check failed: curl http://127.0.0.1:8765/api/state failed\n"),
    ({"STUB_PS": ""}, "panel check failed: expected exactly one running container "
                      "h3-panel-h3-panel-1, found 0\n"),
    ({"STUB_PS": "cid1\\ncid2"}, "panel check failed: expected exactly one running container "
                                 "h3-panel-h3-panel-1, found 2\n"),
])
def test_a_failing_probe_exits_non_zero_with_the_reason(run, env, message):
    done, _ = run(PANEL, **env)
    assert (done.returncode, done.stdout, done.stderr) == (1, "", message)


@pytest.mark.parametrize("env", [{"STUB_PS": "cid1", "STUB_INSPECT_FAIL": "1"},
                                 {"STUB_PS": "cid1", "STUB_GIT_FAIL": "1"}])
def test_a_failing_docker_or_git_call_is_not_swallowed(run, env):
    """`set -e` does not stop on a failure inside `echo "$(…)"` — each value is taken alone."""
    done, _ = run(PANEL, **env)
    assert done.returncode != 0
    assert done.stdout == ""


# --- a Docker with state, for the rollback tag and the image secret check -----------------------

STATEFUL_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
path = os.environ["DOCKER_STATE"]
st = json.load(open(path))
args = sys.argv[1:]
st.setdefault("calls", []).append(" ".join(args))
code = 0
if args[0] == "ps":                                  # ps -q --no-trunc --filter name=^NAME$
    name = args[-1].split("=^", 1)[1].rstrip("$")
    if name in st["containers"]:
        for _ in range(st.get("ps_count", 1)):
            print(name)
elif args[0] == "inspect":                           # inspect -f {{.Image}} CONTAINER
    print(st["containers"][args[-1]])
elif args[:2] == ["image", "inspect"]:               # image inspect -f {{.Id}} REF
    print(st["tags"][args[-1]])
elif args[0] == "tag":
    st["tags"][args[2]] = st["tags"].get(args[1], args[1])
elif args[0] == "build":
    code = st.get("build_rc", 0)
    ctx = args[-1]                                   # what the build would send as its context
    st["build_ctx"] = sorted(os.path.relpath(os.path.join(d, f), ctx)
                             for d, _, fs in os.walk(ctx) for f in fs)
elif args[0] == "run":
    code = st.get("run_rc", 1)
json.dump(st, open(path, "w"))
sys.exit(code)
"""


@pytest.fixture
def docker_state(tmp_path):
    stubs = tmp_path / "sbin"
    stubs.mkdir()
    (stubs / "docker").write_text(STATEFUL_DOCKER)
    (stubs / "docker").chmod(0o755)
    path = tmp_path / "docker.json"

    def run(argv: list[str], state: dict | None = None, cwd: Path | None = None):
        if state is not None:
            path.write_text(json.dumps(state))
        done = subprocess.run(argv, capture_output=True, text=True, cwd=cwd,
                              env={"PATH": f"{stubs}:/usr/bin:/bin", "DOCKER_STATE": str(path),
                                   "HOME": str(tmp_path)})
        return done, json.loads(path.read_text())
    return run


A, B = "sha256:" + "a" * 64, "sha256:" + "b" * 64
STUDIO = "h3-studio-studio-api-1"


def test_rollback_returns_to_the_image_that_was_running_after_a_failed_build_and_a_retry(
        docker_state):
    """Review r3: images, not argv. A runs; `latest` already points at the new B (build passed,
    tests in the image failed, no `up`); the retry tags again; rollback must restore A."""
    _, st = docker_state(["bash", str(PREV), "20261021-1200"],
                         {"containers": {STUDIO: A}, "tags": {"h3-studio:latest": A}})
    st["tags"]["h3-studio:latest"] = B                      # compose build of the new image
    docker_state(["true"], st)
    done, st = docker_state(["bash", str(PREV), "20261021-1230"])
    assert (done.returncode, done.stdout) == (
        0, f"prev {A} -> h3-studio:prev h3-studio:prev-20261021-1230\n")
    _, st = docker_state(["docker", "tag", "h3-studio:prev", "h3-studio:latest"])   # DEPLOY: Откат
    assert st["tags"] == {"h3-studio:latest": A, "h3-studio:prev": A,
                          "h3-studio:prev-20261021-1200": A, "h3-studio:prev-20261021-1230": A}


def test_two_running_studio_containers_fail_and_tag_nothing(docker_state):
    done, st = docker_state(["bash", str(PREV), "20261021-1200"],
                            {"containers": {STUDIO: A}, "tags": {}, "ps_count": 2})
    assert (done.returncode, done.stdout, done.stderr) == (
        1, "", "expected one studio container, found 2\n")
    assert st["tags"] == {}


def test_prev_on_the_first_deploy_tags_nothing(docker_state):
    done, st = docker_state(["bash", str(PREV), "20261021-1200"], {"containers": {}, "tags": {}})
    assert (done.returncode, done.stdout, st["tags"]) == (
        0, "no running studio container: first deploy, nothing to keep\n", {})


SECRETS = ROOT / "scripts" / "check_image_secrets.sh"


@pytest.fixture
def tree(tmp_path):
    """A committed tree with what the script needs: Dockerfile, .dockerignore, a built dist."""
    repo = tmp_path / "repo"
    (repo / "h3_48gb" / "studio" / "web" / "dist").mkdir(parents=True)
    (repo / "Dockerfile.studio").write_text("FROM scratch\n")
    (repo / ".dockerignore").write_text(".env\n**/.env\n")
    (repo / ".gitignore").write_text("dist/\n")
    (repo / "h3_48gb" / "studio" / "web" / "dist" / "index.html").write_text("<!doctype html>")
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "tree"], check=True)
    return repo


@pytest.mark.parametrize("run_rc, code, out, err", [
    (1, 0, "no planted secret in the image\n", ""),
    (0, 1, "", "LEAK: a planted secret is inside the image\n"),
    (125, 2, "", "check failed: the search in the image exited 125\n"),   # docker could not run
    (2, 2, "", "check failed: the search in the image exited 2\n"),       # grep could not read
])
def test_only_not_found_is_a_pass(docker_state, tree, run_rc, code, out, err):
    before = sorted(p.name for p in tree.iterdir())
    done, st = docker_state(["bash", str(SECRETS)], {"containers": {}, "tags": {}, "run_rc": run_rc},
                            cwd=tree)
    assert (done.returncode, done.stdout, done.stderr) == (code, out, err)
    assert sorted(p.name for p in tree.iterdir()) == before      # the working tree is untouched
    assert not (tree / ".env").exists()


def test_a_failed_build_is_a_failed_check(docker_state, tree):
    done, _ = docker_state(["bash", str(SECRETS)], {"containers": {}, "tags": {}, "build_rc": 1},
                           cwd=tree)
    assert done.returncode != 0
    assert "no planted secret" not in done.stdout


DECOYS = {f"{d}{f}" for d in ("", "conf/")
          for f in (".env", ".env.local", "providers.json", "decoy.env", "decoy.pem", "decoy.key",
                    "decoy.sqlite", "decoy.sqlite3", "decoy.db")}


def _ignored(pattern: str, path: str) -> bool:
    """Docker .dockerignore matching for the forms used here: `**/` = any directories (or none),
    `*` = anything but `/`, no leading `**` = anchored at the context root."""
    rx = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
        elif pattern[i] == "*":
            rx += "[^/]*"
            i += 1
        else:
            rx += re.escape(pattern[i])
            i += 1
    return re.fullmatch(rx, path) is not None


def test_only_the_committed_tree_is_the_build_context(docker_state, tree):
    """`git archive HEAD`, not a copy of the working tree: an uncommitted file never reaches it."""
    (tree / "uncommitted-decoy.txt").write_text("not committed\n")
    done, st = docker_state(["bash", str(SECRETS)], {"containers": {}, "tags": {}, "run_rc": 1},
                            cwd=tree)
    assert done.returncode == 0
    assert "uncommitted-decoy.txt" not in st["build_ctx"]
    assert sorted(set(st["build_ctx"]) - DECOYS) == [
        ".dockerignore", ".gitignore", "Dockerfile.studio", "h3_48gb/studio/web/dist/index.html"]


def test_the_planted_decoys_are_exactly_the_expected_set(docker_state, tree):
    done, st = docker_state(["bash", str(SECRETS)], {"containers": {}, "tags": {}, "run_rc": 1},
                            cwd=tree)
    assert {p for p in st["build_ctx"] if p.startswith("conf/") or "/" not in p} & DECOYS == DECOYS
    assert DECOYS - set(st["build_ctx"]) == set()


def test_the_real_dockerignore_ignores_every_decoy():
    rules = [r.strip() for r in (ROOT / ".dockerignore").read_text().splitlines()
             if r.strip() and not r.startswith("#")]
    ignored = {p for p in DECOYS if any(_ignored(r, p) for r in rules)}
    assert ignored == DECOYS


# -- slice S2: the worker container (plan 2026-10-22, task 9) -----------------------------------

def test_the_worker_service_runs_the_worker_on_the_host_network_with_comfy_output_read_only():
    text = (ROOT / "compose.studio.yaml").read_text()
    block = text[text.index("  studio-worker:\n"):]
    assert block == (
        "  studio-worker:\n"
        "    image: h3-studio:latest\n"
        "    network_mode: host\n"
        "    user: \"1000:1000\"\n"
        "    restart: unless-stopped\n"
        "    init: true\n"
        "    stop_grace_period: 40s\n"
        "    command: [\"worker\"]\n"
        "    depends_on:\n"
        "      - studio-api\n"
        "    environment:\n"
        "      TZ: Europe/Moscow\n"
        "      HOME: /tmp\n"
        "      H3_OUTDIR: /home/alex/Outputs\n"
        "    volumes:\n"
        "      - /home/alex/Outputs/studio:/home/alex/Outputs/studio\n"
        "      - /home/alex/Outputs/comfy-h3:/home/alex/Outputs/comfy-h3:ro\n")
    docker = (ROOT / "Dockerfile.studio").read_text()
    assert "apt-get install -y --no-install-recommends tzdata ffmpeg" in docker


def test_the_llm_token_is_mounted_outside_the_data_folder_and_named_by_studio_env_file():
    """Task 10 (coordinator 23.10): a bind mount into /home/alex/Outputs/studio left docker's
    empty root-owned `.env` there; the token now sits at /run/secrets/studio.env, read-only, and
    the API finds it through STUDIO_ENV_FILE. The worker gets neither — it never calls the LLM."""
    text = (ROOT / "compose.studio.yaml").read_text()
    api = text[text.index("  studio-api:\n"):text.index("  # Slice S2")]
    worker = text[text.index("  studio-worker:\n"):]
    mounts = [line.strip() for line in api.splitlines() if line.strip().startswith("- /")]
    assert mounts == ["- /home/alex/Outputs/studio:/home/alex/Outputs/studio",
                      "- /home/alex/Outputs/h3-panel/.env:/run/secrets/studio.env:ro"]
    assert "      STUDIO_ENV_FILE: /run/secrets/studio.env\n" in api
    assert ".env" not in worker and "STUDIO_ENV_FILE" not in worker
