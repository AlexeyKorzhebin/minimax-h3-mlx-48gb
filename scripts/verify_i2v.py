#!/usr/bin/env python3
"""Does a keyframe actually condition the clip, or is it silently ignored?

Runs the same prompt and seed twice at 512x512 — once with a keyframe, once without — and
compares each clip's first frame against the image. The control run is the point: if an
unconditioned clip scores as close to the image as the conditioned one, then what we measured
was agreement with the prompt, not conditioning.

About 50 minutes for the pair. Nothing here is imported by the package.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

OUT = Path.home() / "models/video-out/i2v-check"
PROMPT = "a red vintage car parked on a wet street at night, neon reflections"


def make_keyframe(path: Path) -> None:
    """A frame with structure a generator would not produce by chance."""
    img = Image.new("RGB", (512, 512), (18, 18, 28))
    px = img.load()
    for y in range(512):
        for x in range(512):
            if (x // 64 + y // 64) % 2 == 0:
                px[x, y] = (200, 40, 40)
    img.save(path)


def run(tag: str, image: Path | None) -> Path:
    cmd = ["./.venv/bin/python", "-m", "h3_48gb", "generate", PROMPT,
           "--width", "512", "--height", "512", "--duration", "2.4",
           "--steps", "31", "--seed", "20260807", "--tag", tag, "--outdir", str(OUT)]
    if image is not None:
        cmd += ["--image", str(image)]
    subprocess.run(cmd, check=True)
    return OUT / f"h3-{tag}-512x512.mp4"


def first_frame(clip: Path) -> np.ndarray:
    out = clip.with_suffix(".frame0.png")
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-vframes", "1",
                    "-y", str(out)], check=True)
    return np.asarray(Image.open(out).convert("RGB"), dtype=np.float64)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(((a - b) ** 2).mean())
    return float("inf") if mse == 0 else 20 * np.log10(255.0) - 10 * np.log10(mse)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    key = OUT / "keyframe.png"
    make_keyframe(key)
    reference = np.asarray(Image.open(key).convert("RGB"), dtype=np.float64)

    conditioned = first_frame(run("i2v", key))
    control = first_frame(run("control", None))

    report = {
        "conditioned_psnr": round(psnr(reference, conditioned), 2),
        "control_psnr": round(psnr(reference, control), 2),
        "conditioned_corr": round(float(np.corrcoef(reference.ravel(), conditioned.ravel())[0, 1]), 4),
        "control_corr": round(float(np.corrcoef(reference.ravel(), control.ravel())[0, 1]), 4),
    }
    report["verdict"] = (
        "conditioning works"
        if report["conditioned_psnr"] > report["control_psnr"] + 3
        else "INCONCLUSIVE: the keyframe did not move the first frame measurably"
    )
    (OUT / "verdict.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "conditioning works" else 1


if __name__ == "__main__":
    raise SystemExit(main())
