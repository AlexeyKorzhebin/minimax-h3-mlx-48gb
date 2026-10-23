#!/usr/bin/env python3
"""Make one picture on alex-neuro by hand: take the card, ask the dispatcher, run ComfyUI, print the file.

    python3 make_image.py krea2 "a young woman in a linen dress on a forest path" --seed 11
    python3 make_image.py zimage "..." --size 1024x1024
    python3 make_image.py qwen "..." --profile fast          # Lightning LoRA, 4 steps
    python3 make_image.py krea2 "..." --lora KNP/KNP_000003000.safetensors:0.8
    python3 make_image.py krea2 --prompt-file p.txt --reference photo.jpg --lora KNP/KNP_000003000.safetensors:1.5
        (photo reference through Identity-Edit; needs the ComfyUI-Krea2Edit node pack in ComfyUI)
    add --release to give the card back at the end (otherwise the studio waits until you do)

Runs on the server (ssh alex-neuro). Only stdlib. Templates come from the studio clone.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.request
import uuid
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


IDENTITY_LORA = "Identity-Edit/krea2_identity_edit_v1_2.safetensors"


def shifted_sigmas(steps: int, mu: float = 1.15) -> list[float]:
    """Krea 2 Turbo schedule used by the Test CREA resident session (distilled-fixed mu 1.15)."""
    return [math.exp(mu) / (math.exp(mu) + (1 / (1 - i / steps) - 1)) for i in range(steps)] + [0.0]


def upload_image(path: str) -> str:
    """POST /upload/image (multipart) and return the name ComfyUI stored it under."""
    boundary = "----b" + uuid.uuid4().hex
    name = "owner_" + uuid.uuid4().hex[:8] + Path(path).suffix.lower()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode() + Path(path).read_bytes() + (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n--{boundary}--\r\n').encode()
    req = urllib.request.Request(f"{COMFY}/upload/image", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["name"]


def identity_graph(image_name: str, prompt: str, seed: int, width: int, height: int, steps: int,
                   ref_boost: float, grounding_px: int, knp: dict | None, prefix: str) -> dict:
    """Krea 2 Turbo INT8 + Identity-Edit (bypass LoRA) + optional personal LoRA, API format.

    Identity-Edit must be a *bypass* LoRA (an INT8 base cannot be merged); a second *bypass* LoRA would
    replace it (same injection key), so the personal LoRA goes through the regular LoraLoaderModelOnly.
    Verified live 23.10: knight/archmage portraits match the resident session.
    """
    sigmas = ", ".join(f"{s:.6f}" for s in shifted_sigmas(steps))
    model = ["5", 0]
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_int8_convrot.safetensors", "type": "krea2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "LoraLoaderBypassModelOnly", "inputs": {"model": ["1", 0], "lora_name": IDENTITY_LORA, "strength_model": 1.0}},
    }
    if knp:
        graph["6"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["5", 0], "lora_name": knp["name"], "strength_model": knp["strength"]}}
        model = ["6", 0]
    graph.update({
        "7": {"class_type": "Krea2EditGroundedEncode", "inputs": {"clip": ["2", 0], "prompt": prompt, "image": ["4", 0], "grounding_px": grounding_px}},
        "8": {"class_type": "Krea2EditGroundedEncode", "inputs": {"clip": ["2", 0], "prompt": "", "image": ["4", 0], "grounding_px": grounding_px}},
        "9": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "10": {"class_type": "Krea2EditModelPatch", "inputs": {"model": model, "source_latent": ["9", 0], "ref_boost": ref_boost, "fit_mode": "fit", "vae": ["3", 0], "source_image": ["4", 0], "target_latent": ["9", 0]}},
        "11": {"class_type": "ManualSigmas", "inputs": {"sigmas": sigmas}},
        "12": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "13": {"class_type": "SamplerCustom", "inputs": {"model": ["10", 0], "add_noise": True, "noise_seed": seed, "cfg": 1.0, "positive": ["7", 0], "negative": ["8", 0], "sampler": ["12", 0], "sigmas": ["11", 0], "latent_image": ["9", 0]}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["3", 0]}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": prefix}},
    })
    return graph


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
    ap.add_argument("prompt", nargs="?", default=None, help="текст; или --prompt-file")
    ap.add_argument("--prompt-file", default=None, help="прочитать промпт из файла")
    ap.add_argument("--reference", default=None, help="фото-референс на сервере (только krea2): Identity-Edit")
    ap.add_argument("--ref-boost", type=float, default=2.0, help="сила связи с фото, 1-8 (с --reference)")
    ap.add_argument("--grounding-px", type=int, default=1024, choices=[640, 768, 1024], help="(с --reference)")
    ap.add_argument("--machine", action="store_true", help="печатать строки RESULT<TAB>путь<TAB>секунды для скриптов")
    ap.add_argument("--seed", type=int, default=None, help="по умолчанию случайный, печатается")
    ap.add_argument("--size", default="1344x768", help="ШxВ, кратно 64 (Qwen — 16)")
    ap.add_argument("--profile", choices=["fast", "full"], default="full", help="только qwen")
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--negative", default="", help="только qwen «Полное»")
    ap.add_argument("--lora", default=None, help="имя:сила (krea2, qwen)")
    ap.add_argument("--release", action="store_true", help="отдать карту в конце")
    args = ap.parse_args()
    if args.prompt_file:
        args.prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not args.prompt:
        ap.error("нужен текст промпта или --prompt-file")
    if args.reference and args.model != "krea2":
        ap.error("--reference поддержан только для krea2")

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

    take_card()
    if args.reference:
        if "Krea2EditModelPatch" not in call(f"{COMFY}/object_info/Krea2EditModelPatch"):
            if args.release:
                call(f"{DISPATCHER}/release", {"client": CLIENT})
            sys.exit("в ComfyUI нет узлов ComfyUI-Krea2Edit (custom_nodes/ComfyUI-Krea2Edit): фото-референс не поддержан")
        knp = values.get("lora")
        payload = {"prompt": identity_graph(upload_image(args.reference), args.prompt, seed, width, height,
                                            args.steps or 12, args.ref_boost, args.grounding_px, knp,
                                            f"owner/{task_id}/f"),
                   "client_id": "owner-make-image"}
    else:
        payload = comfy_template.build_payload(graph, card, values)
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
            if args.machine:
                print(f"RESULT\t{path}\t{time.time() - started:.1f}", flush=True)
    if args.release:
        call(f"{DISPATCHER}/release", {"client": CLIENT})
        print("карта отдана")
    else:
        print("карта остаётся за вами — отдать: python3 make_image.py --help (флаг --release) или "
              "curl -s -X POST 127.0.0.1:8790/release -H 'Content-Type: application/json' "
              "-d '{\"client\":\"owner\"}'")


if __name__ == "__main__":
    main()
