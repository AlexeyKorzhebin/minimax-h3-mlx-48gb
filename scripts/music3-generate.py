#!/usr/bin/env python3
"""Generate one MiniMax Music 3 track on MLX and report timing + peak memory.

Lives in the `music3-venv` (`~/models/music3/.venv`), not this repo's own `.venv`: `mlx_audio`'s
music stack is a separate install from `h3_48gb`'s, and `h3_48gb.songrun` (which drives this
script) never imports `mlx`/`mlx_audio` itself -- it only ever reaches this generation through a
subprocess, the same "no mlx in the orchestrating process" discipline `h3_48gb.worker` documents
for video jobs. `songrun.run_song` invokes this file with the `music3_python` interpreter it is
given, exactly the way it would be invoked by hand:
`<music3_python> scripts/music3-generate.py --model ... --caption-file ... --lyrics-file ...`.

Versioned copy of `~/models/music3/gen.py` (2026-08-17 experiment) -- behaviour unchanged, output
lines unchanged (`songrun._generate_wav` parses the final `[audio] ... dur=<seconds>s` line to
learn how long Music3 actually sang, since Music3 stops early once the lyrics run out and the
number is not knowable from `--duration` alone -- see that function's own docstring).
"""

import argparse
import time
from pathlib import Path

import mlx.core as mx
from mlx_audio.audio_io import write as audio_write
from mlx_audio.music import load_model


def peak_gb():
    for fn in ("get_peak_memory", "metal.get_peak_memory"):
        obj = mx
        try:
            for part in fn.split("."):
                obj = getattr(obj, part)
            return obj() / 1e9
        except AttributeError:
            continue
    return float("nan")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=str(Path.home() / "models/music3/mlx-8bit"))
    p.add_argument("--caption-file", required=True)
    p.add_argument("--lyrics-file", required=True)
    p.add_argument("--duration", type=float, default=90.0)
    p.add_argument("--steps", type=int, default=30)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    caption = Path(args.caption_file).read_text(encoding="utf-8")
    lyrics = Path(args.lyrics_file).read_text(encoding="utf-8")

    # Must be a Path, not a str: base_load_model() sends str through
    # get_model_path(), which tries to resolve it as a HF repo id and hangs.
    t0 = time.time()
    model = load_model(Path(args.model))
    t_load = time.time() - t0
    print(f"[timing] model load: {t_load:.1f} s", flush=True)
    print(f"[mem] after load, peak: {peak_gb():.2f} GB", flush=True)

    t1 = time.time()
    chunks, sr = [], None
    for result in model.generate(
        text=caption,
        lyrics=lyrics,
        duration=args.duration,
        steps=args.steps,
        seed=args.seed,
    ):
        sr = result.sample_rate if sr is None else sr
        chunks.append(result.audio)
    t_gen = time.time() - t1

    audio = chunks[0] if len(chunks) == 1 else mx.concatenate(chunks, axis=0)
    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    audio_write(out, audio, sr)

    secs = audio.shape[0] / sr
    print(f"[timing] generation: {t_gen:.1f} s", flush=True)
    print(f"[timing] TOTAL: {t_load + t_gen:.1f} s", flush=True)
    print(f"[mem] peak: {peak_gb():.2f} GB", flush=True)
    print(f"[audio] {out}  shape={audio.shape}  sr={sr}  dur={secs:.1f}s", flush=True)
    print(f"[ratio] {t_gen / secs:.2f}x realtime", flush=True)


if __name__ == "__main__":
    main()
