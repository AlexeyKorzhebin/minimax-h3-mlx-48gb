#!/usr/bin/env python3
"""Make one picture on alex-neuro by hand: take the card, ask the dispatcher, run ComfyUI, print the file.

    python3 make_image.py krea2 "a young woman in a linen dress on a forest path" --seed 11
    python3 make_image.py zimage "..." --size 1024x1024
    python3 make_image.py qwen "..." --profile fast          # Lightning LoRA, 4 steps
    python3 make_image.py krea2 "..." --lora KNP/KNP_000003000.safetensors:0.8
    add --release to give the card back at the end (otherwise the studio waits until you do)

Runs on the server (ssh alex-neuro). Only stdlib. Templates come from the studio clone.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

TEMPLATES = Path("/home/alex/Projects/h3-studio/docs/h3-modes/comfy-templates")
OUTPUTS = Path("/home/alex/Outputs/comfy-h3")
DISPATCHER = "http://127.0.0.1:8790"
COMFY = "http://127.0.0.1:8189"
MODELS = {"krea2": "krea2-t2i", "zimage": "zimage-turbo-t2i", "qwen": "qwen-image-t2i"}
CLIENT = "owner"

sys.path.insert(0, str(TEMPLATES))
import comfy_template  # noqa: E402  (the studio's own substituter)


def call(url: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def take_card() -> None:
    while True:
        answer = call(f"{DISPATCHER}/acquire", {"client": CLIENT, "engine": "comfy-studio"})
        state = answer.get("state")
        if state == "ready":
            return
        if state == "failed":
            sys.exit(f"ComfyUI не поднялся, лог: {answer.get('log')}")
        print(f"карта: {state} {answer.get('reason', '')}".rstrip(), flush=True)
        time.sleep(5)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", choices=sorted(MODELS))
    ap.add_argument("prompt")
    ap.add_argument("--seed", type=int, default=None, help="по умолчанию случайный, печатается")
    ap.add_argument("--size", default="1344x768", help="ШxВ, кратно 64 (Qwen — 16)")
    ap.add_argument("--profile", choices=["fast", "full"], default="full", help="только qwen")
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--negative", default="", help="только qwen «Полное»")
    ap.add_argument("--lora", default=None, help="имя:сила (krea2, qwen)")
    ap.add_argument("--release", action="store_true", help="отдать карту в конце")
    args = ap.parse_args()

    template_id = MODELS[args.model]
    card_path = TEMPLATES / f"{template_id}.card.json"
    card = json.loads(card_path.read_text())
    graph = json.loads((TEMPLATES / card["graph"]).read_text())
    width, height = (int(v) for v in args.size.lower().split("x"))
    seed = args.seed if args.seed is not None else random.randint(1, 2**31 - 1)
    task_id = f"owner-{int(time.time())}"
    values: dict = {"task_id": task_id, "prompt": args.prompt, "seed": seed, "width": width, "height": height}
    inputs = card.get("inputs", {})
    limits = card["requires_json"].get("limits", {})
    qwen_profiles = limits.get("profiles") or {   # the S2b card carries these; older cards do not
        "fast": {"steps": 4, "cfg": 1.0,
                 "lora": {"name": "Qwen-Image-2512-Lightning-4steps-V1.0-bf16.safetensors", "strength": 1.0}},
        "full": {"steps": 50, "cfg": 4.0, "lora": None}}
    profile = qwen_profiles.get(args.profile) if args.model == "qwen" else None
    if profile:
        values["steps"], values["cfg"] = profile["steps"], profile["cfg"]
        if profile.get("lora"):
            values["lora"] = profile["lora"]
    if args.steps is not None:
        values["steps"] = args.steps
    if "negative" in inputs and args.negative:
        values["negative"] = args.negative
    if args.lora:
        name, _, strength = args.lora.partition(":")
        values["lora"] = {"name": name, "strength": float(strength or 1.0)}

    payload = comfy_template.build_payload(graph, card, values)
    take_card()
    started = time.time()
    prompt_id = call(f"{COMFY}/prompt", payload)["prompt_id"]
    print(f"модель {args.model}, сид {seed}, {width}x{height}, задача {prompt_id}", flush=True)
    while True:
        time.sleep(1)
        entry = call(f"{COMFY}/history/{prompt_id}").get(prompt_id)
        if not entry:
            continue
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            sys.exit(f"ComfyUI: ошибка задачи {prompt_id}: {json.dumps(status)[:500]}")
        if status.get("completed"):
            break
    for node in entry.get("outputs", {}).values():
        for image in node.get("images", []):
            path = OUTPUTS / image.get("subfolder", "") / image["filename"]
            print(f"готово за {time.time() - started:.1f} с: {path}")
            print(f"с Мака: /Volumes/home/Outputs/comfy-h3/{image.get('subfolder', '')}/{image['filename']}")
    if args.release:
        call(f"{DISPATCHER}/release", {"client": CLIENT})
        print("карта отдана")
    else:
        print("карта остаётся за вами — отдать: python3 make_image.py --help (флаг --release) или "
              "curl -s -X POST 127.0.0.1:8790/release -H 'Content-Type: application/json' "
              "-d '{\"client\":\"owner\"}'")


if __name__ == "__main__":
    main()
