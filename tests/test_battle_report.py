"""tools/battle_report.py (final review 2026-10-07, I7): the markdown a battle run is reported
with, built only from what the panel and the dispatcher write. Exact text: a report that reads
fine and puts the H3 wall in the inference column is exactly what this must catch."""
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("battle_report", ROOT / "tools" / "battle_report.py")
br = importlib.util.module_from_spec(_spec)
sys.modules["battle_report"] = br
_spec.loader.exec_module(br)


@pytest.fixture
def moscow(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _epoch(local: str) -> float:
    return datetime.fromisoformat(local).timestamp()


def _battle(tmp_path):
    out = tmp_path / "h3-panel"
    pdir = out / "projects" / "20261008-1000-boi"
    pj = pdir / "project.json"
    queue = out / "queue"
    scenes = []
    for idx, (job, start, end, wait, engine) in enumerate([
            ("j0", "10:00:10", "10:20:10", 95.0, 92.0), ("j1", "10:20:12", "10:38:12", 0.4, None)]):
        stem = str(pdir / "scenes" / f"h3-scene-{idx}-896x512")
        _write(queue / "done" / f"{job}.json", {
            "id": job, "kind": "generate", "args": ["generate", "p"], "output_stem": stem,
            "started_at": f"2026-10-08T{start}", "finished_at": f"2026-10-08T{end}",
            "gpu_wait_s": wait, "engine_start_s": engine})
        _write(Path(stem + ".json"), {
            "engine": "sglang", "status": "completed", "wall_s": 1100.0 + idx,
            "inference_time_s": 1060.5, "peak_memory_mb": 61440.0, "server_s": 1080.0,
            "download_s": 3.0, "framecheck_s": 17.0})
        scenes.append({"idx": idx, "prompt": "@amazon", "duration": 8.0 if idx == 0 else 7.958,
                       "status": "done", "job_id": job, "clip_path": stem + ".mp4"})
    _write(queue / "done" / "u1.json", {
        "id": "u1", "kind": "upscale", "args": ["upscale", "--project", str(pj)],
        "started_at": "2026-10-08T10:38:13", "finished_at": "2026-10-08T10:47:13",
        "gpu_wait_s": 160.0, "engine_start_s": 40.0})
    _write(queue / "done" / "a0.json", {
        "id": "a0", "kind": "assemble", "args": ["assemble", "--project", str(pj), "--draft"],
        "started_at": "2026-10-08T10:50:00", "finished_at": "2026-10-08T10:50:30"})
    _write(queue / "done" / "a1.json", {
        "id": "a1", "kind": "assemble", "args": ["assemble", "--project", str(pj)],
        "started_at": "2026-10-08T10:47:14", "finished_at": "2026-10-08T10:48:04"})
    _write(pdir / "upscale" / "report.json", {
        "attempt": "u1-1", "strength": 0.3, "motion": 4.217, "motion_s": 6.0, "status": "done",
        "parts": [{"idx": 0, "src_frames": 192, "pad_frames": 193, "pad_s": 4.0, "upload_s": 1.0,
                   "comfy_s": 200.0, "mux_s": 12.0, "wall_s": 217.0}]})
    _write(pj, {"id": "20261008-1000-boi", "kind": "video", "title": "Бой", "scenes": scenes,
                "assembly": {"final_path": str(pdir / "assembly" / "final.mp4")},
                "stage_times": {"script": {"approved": "2026-10-08T10:00:05"},
                                "scenes": {"done": "2026-10-08T10:38:12"},
                                "upscale": {"done": "2026-10-08T10:47:13"},
                                "assembly": {"done": "2026-10-08T10:48:04"}}})
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(json.dumps(e) for e in [
        {"ts": _epoch("2026-10-08T09:00:00"), "event": "starting", "engine": "h3",
         "client": "probes", "pid": 1},
        {"ts": _epoch("2026-10-08T10:00:12"), "event": "starting", "engine": "h3",
         "client": "panel-worker", "pid": 7},
        {"ts": _epoch("2026-10-08T10:01:44"), "event": "ready", "engine": "h3",
         "client": "panel-worker", "pid": 7, "seconds": 92.0},
        {"ts": _epoch("2026-10-08T10:38:14"), "event": "stopping", "engine": "h3",
         "client": "panel-worker", "pid": 7},
        {"ts": _epoch("2026-10-08T10:38:26"), "event": "stopped", "engine": "h3",
         "client": "panel-worker", "pid": 7, "seconds": 12.0, "sigkill": False},
        {"ts": _epoch("2026-10-08T11:30:00"), "event": "release", "engine": None,
         "client": "panel-worker", "all": False, "stopped": ["ltx"]}]) + "\n", encoding="utf-8")
    return pdir, queue, events


EXPECTED = """# Боевой прогон: Бой

Проект `20261008-1000-boi`, `{pj}`.

## Сцены H3

| # | задача | длит., с | статус | ожидание GPU, с | подъём H3, с | H3 wall, с | inference, с | сервер, с | скачивание, с | проверка, с | peak, ГБ | задача целиком |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | `j0` | 8.000 | done | 95.0 | 92.0 | 1100.0 | 1060.5 | 1080.0 | 3.0 | 17.0 | 60.00 | 20 мин 00 с |
| 1 | `j1` | 7.958 | done | 0.4 | — | 1101.0 | 1060.5 | 1080.0 | 3.0 | 17.0 | 60.00 | 18 мин 00 с |

Итого H3 wall 36 мин 41 с, ожидание GPU 1 мин 35 с (из него подъём H3 1 мин 32 с).

## Апскейл LTX

Сила 0.3, движение 4.22 (замер 6.0 с), статус done; ожидание GPU 160.0 с, подъём ComfyUI 40.0 с, задача целиком 9 мин 00 с.

| # | кадры | pad, с | upload, с | ComfyUI, с | mux, с | часть, с |
|---|---|---|---|---|---|---|
| 0 | 192→193 | 4.0 | 1.0 | 200.0 | 12.0 | 217.0 |

## Сборка

Задача `a1` (done): 0 мин 50 с, `{final}`.

## Переключения движков (диспетчер)

| время | событие | движок | клиент | длительность, с | примечание |
|---|---|---|---|---|---|
| 10:00:12 | starting | H3 | panel-worker | — |  |
| 10:01:44 | ready | H3 | panel-worker | 92.0 |  |
| 10:38:14 | stopping | H3 | panel-worker | — |  |
| 10:38:26 | stopped | H3 | panel-worker | 12.0 |  |

## Итог

| этап | время |
|---|---|
| постановка (сценарий утверждён) | 2026-10-08T10:00:05 |
| сцены готовы | 2026-10-08T10:38:12 |
| апскейл готов | 2026-10-08T10:47:13 |
| финальный файл | 2026-10-08T10:48:04 |
| от постановки до финального файла | 47 мин 59 с |
"""


def test_the_report_of_a_battle_project_is_exact(tmp_path, moscow):
    pdir, queue, events = _battle(tmp_path)
    text = br.build_report(pdir, queue=queue, events_path=events)
    assert text == EXPECTED.format(pj=pdir / "project.json",
                                   final=pdir / "assembly" / "final.mp4")


def test_the_cli_finds_the_queue_next_to_the_projects_and_writes_the_file(tmp_path, moscow):
    pdir, queue, events = _battle(tmp_path)
    out = tmp_path / "BATTLE.md"
    assert br.main([str(pdir), "--events", str(events), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == br.build_report(pdir, queue=queue,
                                                              events_path=events)
