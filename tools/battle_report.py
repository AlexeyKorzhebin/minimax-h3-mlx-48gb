#!/usr/bin/env python3
"""Battle report of one panel project as a markdown table (final review 2026-10-07, I7).

    python3 tools/battle_report.py /home/alex/Outputs/h3-panel/projects/<id> \
        [--queue DIR] [--events FILE] [--out FILE]

Stdlib only, reads what the panel and the dispatcher already write -- nothing is measured here:

- `project.json`: scenes (each scene's current `job_id`), `stage_times`, `assembly.final_path`;
- the queue (`<outdir>/queue`, `done/` and `failed/`): per job `started_at`/`finished_at`, the
  GPU gate's `gpu_wait_s`/`engine_start_s`;
- each scene's `<stem>.json` (sglang adapter): `wall_s`, `inference_time_s`, `peak_memory_mb`,
  `server_s`, `download_s`, `framecheck_s`;
- `<project>/upscale/report.json` (LTX adapter): strength, motion, per-part times;
- the dispatcher's `events.jsonl` (host, next to its state.json): every engine start and stop.

Queue and project timestamps are naive local time of the panel container (TZ=Europe/Moscow in
compose.yaml); dispatcher events are epoch seconds. Run this with the same TZ as the container
(`TZ=Europe/Moscow python3 tools/battle_report.py ...`) so the two line up.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_EVENTS = Path.home() / ".local/state/h3-gpu-dispatcher/events.jsonl"
ENGINE_NAMES = {"h3": "H3", "ltx": "ComfyUI"}


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    moment = datetime.fromisoformat(iso)
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _seconds(start: str | None, end: str | None) -> float | None:
    a, b = _local(start), _local(end)
    return None if a is None or b is None else (b - a).total_seconds()


def _num(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours} ч {minutes:02d} мин {secs:02d} с" if hours else f"{minutes} мин {secs:02d} с"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def load_jobs(queue: Path) -> dict[str, dict]:
    jobs = {}
    for state in ("done", "failed", "running", "pending"):
        for path in sorted((queue / state).glob("*.json")):
            data = _read_json(path)
            if isinstance(data, dict) and "id" in data:
                jobs[data["id"]] = {**data, "state": state}
    return jobs


def load_events(path: Path | None) -> list[dict]:
    if path is None or not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def _project_jobs(jobs: dict, project_json: Path, kind: str) -> list[dict]:
    """Jobs of `kind` whose args name this project, oldest first; drafts are not the battle."""
    found = [job for job in jobs.values()
             if job.get("kind") == kind and str(project_json) in job.get("args", [])
             and "--draft" not in job.get("args", [])]
    return sorted(found, key=lambda job: job.get("started_at") or job.get("created_at") or "")


def build_report(project_dir: Path, *, queue: Path, events_path: Path | None) -> str:
    project_json = project_dir / "project.json"
    proj = _read_json(project_json)
    if not isinstance(proj, dict):
        raise SystemExit(f"не читается {project_json}")
    jobs = load_jobs(queue)
    stage_times = proj.get("stage_times") or {}
    scenes = sorted(proj.get("scenes") or [], key=lambda scene: scene["idx"])

    out = [f"# Боевой прогон: {proj.get('title') or proj['id']}", "",
           f"Проект `{proj['id']}`, `{project_json}`.", ""]

    # -- scenes ---------------------------------------------------------------------------------
    rows, h3_total, wait_total, start_total = [], 0.0, 0.0, 0.0
    first_started = None
    for scene in scenes:
        job = jobs.get(scene.get("job_id") or "") or {}
        report = _read_json(Path(job["output_stem"] + ".json")) if job.get("output_stem") else None
        report = report if isinstance(report, dict) else {}
        if job.get("started_at") and (first_started is None or job["started_at"] < first_started):
            first_started = job["started_at"]
        h3_total += report.get("wall_s") or 0.0
        wait_total += job.get("gpu_wait_s") or 0.0
        start_total += job.get("engine_start_s") or 0.0
        peak = report.get("peak_memory_mb")
        rows.append([str(scene["idx"]), f"`{scene.get('job_id') or '—'}`",
                     _num(scene.get("duration"), 3), scene.get("status") or "—",
                     _num(job.get("gpu_wait_s")), _num(job.get("engine_start_s")),
                     _num(report.get("wall_s")), _num(report.get("inference_time_s")),
                     _num(report.get("server_s")), _num(report.get("download_s")),
                     _num(report.get("framecheck_s")),
                     _num(peak / 1024 if peak is not None else None, 2),
                     _duration(_seconds(job.get("started_at"), job.get("finished_at")))])
    out += ["## Сцены H3", ""]
    out += _table(["#", "задача", "длит., с", "статус", "ожидание GPU, с", "подъём H3, с",
                   "H3 wall, с", "inference, с", "сервер, с", "скачивание, с", "проверка, с",
                   "peak, ГБ", "задача целиком"], rows)
    out += ["", f"Итого H3 wall {_duration(h3_total)}, ожидание GPU {_duration(wait_total)} "
                f"(из него подъём H3 {_duration(start_total)}).", ""]

    # -- upscale --------------------------------------------------------------------------------
    upscale = _read_json(project_dir / "upscale" / "report.json")
    upscale_jobs = _project_jobs(jobs, project_json, "upscale")
    out += ["## Апскейл LTX", ""]
    if isinstance(upscale, dict):
        job = upscale_jobs[-1] if upscale_jobs else {}
        out += [f"Сила {upscale.get('strength')}, движение {_num(upscale.get('motion'), 2)} "
                f"(замер {_num(upscale.get('motion_s'))} с), статус {upscale.get('status')}; "
                f"ожидание GPU {_num(job.get('gpu_wait_s'))} с, подъём ComfyUI "
                f"{_num(job.get('engine_start_s'))} с, задача целиком "
                f"{_duration(_seconds(job.get('started_at'), job.get('finished_at')))}.", ""]
        out += _table(["#", "кадры", "pad, с", "upload, с", "ComfyUI, с", "mux, с", "часть, с"],
                      [[str(part.get("idx")),
                        f"{part.get('src_frames', '—')}→{part.get('pad_frames', '—')}",
                        _num(part.get("pad_s")), _num(part.get("upload_s")),
                        _num(part.get("comfy_s")), _num(part.get("mux_s")),
                        _num(part.get("wall_s"))] for part in upscale.get("parts") or []])
        out.append("")
    else:
        out += ["Апскейла не было (нет upscale/report.json).", ""]

    # -- assembly -------------------------------------------------------------------------------
    assemblies = _project_jobs(jobs, project_json, "assemble")
    out += ["## Сборка", ""]
    if assemblies:
        job = assemblies[-1]
        out += [f"Задача `{job['id']}` ({job['state']}): "
                f"{_duration(_seconds(job.get('started_at'), job.get('finished_at')))}, "
                f"`{(proj.get('assembly') or {}).get('final_path') or '—'}`.", ""]
    else:
        out += ["Сборки не было.", ""]

    # -- engine switches ------------------------------------------------------------------------
    finished = (stage_times.get("assembly") or {}).get("done")
    start = (stage_times.get("script") or {}).get("approved") or first_started
    window = (_local(start), _local(finished) or datetime.max)
    switches = [event for event in load_events(events_path)
                if window[0] is not None
                and window[0] <= datetime.fromtimestamp(event["ts"]) <= window[1]]
    out += ["## Переключения движков (диспетчер)", ""]
    if switches:
        out += _table(["время", "событие", "движок", "клиент", "длительность, с", "примечание"],
                      [[datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S"), e["event"],
                        ENGINE_NAMES.get(e.get("engine"), e.get("engine") or "—"),
                        e.get("client") or "—", _num(e.get("seconds")),
                        ("SIGKILL" if e.get("sigkill") else "")
                        + (e.get("reason") or "")
                        + (f"остановлены: {', '.join(e['stopped'])}" if e.get("stopped") else "")]
                       for e in switches])
    else:
        out += [f"Событий нет ({events_path or 'events.jsonl не задан'}).", ""]
    out.append("")

    # -- total ----------------------------------------------------------------------------------
    out += ["## Итог", ""]
    out += _table(["этап", "время"], [
        ["постановка (сценарий утверждён)", start or "—"],
        ["сцены готовы", (stage_times.get("scenes") or {}).get("done") or "—"],
        ["апскейл готов", (stage_times.get("upscale") or {}).get("done") or "—"],
        ["финальный файл", finished or "—"],
        ["от постановки до финального файла", _duration(_seconds(start, finished))]])
    out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--queue", type=Path, help="default: <outdir>/queue")
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--out", type=Path, help="write the markdown here instead of stdout")
    args = parser.parse_args(argv)
    project_dir = args.project_dir.absolute()
    queue = args.queue or project_dir.parent.parent / "queue"
    text = build_report(project_dir, queue=queue, events_path=args.events)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
