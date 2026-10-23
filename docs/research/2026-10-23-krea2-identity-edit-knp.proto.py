#!/usr/bin/env python3
"""Prototype: Krea 2 Turbo INT8 + photo reference (Identity-Edit) + KNP in ComfyUI. Runs on alex-neuro (stdlib only).
usage: comfy_krea_ref.py graph|run --photo P --prompt-file F --seed N [--size WxH] [--ref-boost 2] [--knp 1.5]
"""
import argparse, json, math, sys, time, uuid, urllib.request
from pathlib import Path
COMFY = "http://127.0.0.1:8189"
KNP_NODE = "LoraLoaderModelOnly"

def shifted_sigmas(steps, mu):
    return [math.exp(mu) / (math.exp(mu) + (1 / (1 - i / steps) - 1)) for i in range(steps)] + [0.0]

def graph(image_name, prompt, seed, w, h, steps=12, ref_boost=2.0, grounding=1024, knp=1.5, identity=1.0, prefix="testcrea/img"):
    sig = ", ".join(f"{s:.6f}" for s in shifted_sigmas(steps, 1.15))
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_int8_convrot.safetensors", "type": "krea2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "LoraLoaderBypassModelOnly", "inputs": {"model": ["1", 0], "lora_name": "Identity-Edit/krea2_identity_edit_v1_2.safetensors", "strength_model": identity}},
        "6": {"class_type": KNP_NODE, "inputs": {"model": ["5", 0], "lora_name": "KNP/KNP_000003000.safetensors", "strength_model": knp}},
        "7": {"class_type": "Krea2EditGroundedEncode", "inputs": {"clip": ["2", 0], "prompt": prompt, "image": ["4", 0], "grounding_px": grounding}},
        "8": {"class_type": "Krea2EditGroundedEncode", "inputs": {"clip": ["2", 0], "prompt": "", "image": ["4", 0], "grounding_px": grounding}},
        "9": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "10": {"class_type": "Krea2EditModelPatch", "inputs": {"model": ["6", 0], "source_latent": ["9", 0], "ref_boost": ref_boost, "fit_mode": "fit", "vae": ["3", 0], "source_image": ["4", 0], "target_latent": ["9", 0]}},
        "11": {"class_type": "ManualSigmas", "inputs": {"sigmas": sig}},
        "12": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "13": {"class_type": "SamplerCustom", "inputs": {"model": ["10", 0], "add_noise": True, "noise_seed": seed, "cfg": 1.0, "positive": ["7", 0], "negative": ["8", 0], "sampler": ["12", 0], "sigmas": ["11", 0], "latent_image": ["9", 0]}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["3", 0]}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": prefix}},
    }

def upload(path):
    b = "----b" + uuid.uuid4().hex
    name = "testcrea_" + uuid.uuid4().hex[:8] + Path(path).suffix
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + Path(path).read_bytes() + f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{b}--\r\n".encode()
    r = urllib.request.Request(COMFY + "/upload/image", data=body, headers={"Content-Type": "multipart/form-data; boundary=" + b})
    return json.loads(urllib.request.urlopen(r).read())["name"]

def call(url, body=None):
    d = None if body is None else json.dumps(body).encode()
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=d, headers={"Content-Type": "application/json"}), timeout=60).read())

def main():
    global KNP_NODE
    ap = argparse.ArgumentParser(); ap.add_argument("cmd"); ap.add_argument("--photo"); ap.add_argument("--prompt-file"); ap.add_argument("--seed", type=int, default=145)
    ap.add_argument("--size", default="864x1152"); ap.add_argument("--ref-boost", type=float, default=2.0); ap.add_argument("--knp", type=float, default=1.5); ap.add_argument("--steps", type=int, default=12); ap.add_argument("--grounding", type=int, default=1024); ap.add_argument("--identity", type=float, default=1.0); ap.add_argument("--tag", default="s")
    ap.add_argument("--knp-node", default="LoraLoaderModelOnly"); a = ap.parse_args(); KNP_NODE = a.knp_node; w, h = map(int, a.size.split("x"))
    prompt = Path(a.prompt_file).read_text().strip()
    if a.cmd == "graph":
        print(json.dumps(graph("REF.jpg", prompt, a.seed, w, h, a.steps, a.ref_boost, a.grounding, a.knp), indent=1, ensure_ascii=False)); return
    img = upload(a.photo)
    t0 = time.time()
    pid = call(COMFY + "/prompt", {"prompt": graph(img, prompt, a.seed, w, h, a.steps, a.ref_boost, a.grounding, a.knp, a.identity, prefix=f"testcrea/{a.tag}{a.seed}"), "client_id": "owner-testcrea"})["prompt_id"]
    while True:
        hist = call(f"{COMFY}/history/{pid}")
        if pid in hist and hist[pid].get("status", {}).get("completed") is not None:
            break
        time.sleep(2)
    st = hist[pid]["status"]; print("status", st.get("status_str"), "sec", round(time.time() - t0, 1))
    if st.get("status_str") != "success": print(json.dumps(st)[:3000]); sys.exit(1)
    for n in hist[pid]["outputs"].values():
        for i in n.get("images", []): print("FILE", "/home/alex/Projects/comfy-h3/output/" + i.get("subfolder", "") + "/" + i["filename"])
main()
