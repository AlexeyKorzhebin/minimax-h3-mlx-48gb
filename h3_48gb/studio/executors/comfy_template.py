"""Pure substituter for ComfyUI studio templates (spec section 8.2).

build_payload(graph, card, values) -> {"prompt": graph, "client_id": task_id, "extra_data": {...}}

No I/O, no network, stdlib only. The graph is an API-format ComfyUI graph with default values;
the card lists, per input name, the (node, field) pairs to overwrite. Raises TemplateError on any
unknown, missing or out-of-range value, so a bad request fails before it reaches the GPU.
CLI: python comfy_template.py CARD.json VALUES.json [GRAPH.json]  -> payload JSON on stdout.
"""
import copy
import json
import os
import re
import sys


class TemplateError(ValueError):
    pass


def snap_length(seconds, fps=24, grid_step=17, grid_offset=5):
    """Frames for a duration, rounded UP onto the model's 17k+5 grid (same rule as the node)."""
    fr = max(grid_offset, round(seconds * fps))
    return fr + (grid_offset - fr % grid_step) % grid_step


def _check_scalar(name, spec, v):
    t = spec["type"]
    if t == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            raise TemplateError(f"{name}: expected int, got {v!r}")
    elif t == "float":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TemplateError(f"{name}: expected number, got {v!r}")
    elif t in ("string", "image", "model_file"):
        if not isinstance(v, str) or v == "":
            raise TemplateError(f"{name}: expected non-empty string, got {v!r}")
    elif t == "enum":
        if v not in spec["options"]:
            raise TemplateError(f"{name}: {v!r} not in {spec['options']}")
    if "min" in spec and v < spec["min"]:
        raise TemplateError(f"{name}: {v} < min {spec['min']}")
    if "max" in spec and v > spec["max"]:
        raise TemplateError(f"{name}: {v} > max {spec['max']}")
    if "multiple_of" in spec and v % spec["multiple_of"]:
        raise TemplateError(f"{name}: {v} is not a multiple of {spec['multiple_of']}")
    if "grid" in spec:
        step, off = spec["grid"]["step"], spec["grid"]["offset"]
        if (v - off) % step:
            raise TemplateError(f"{name}: {v} is off the {step}k+{off} grid (nearest up: {snap_length(v / 24)})")
    if "pattern" in spec and not re.fullmatch(spec["pattern"], v):
        raise TemplateError(f"{name}: {v!r} does not match {spec['pattern']}")


def ref_image_limit(card, frames):
    """Max reference images for a clip of `frames` frames, from requires_json.limits.ref_images_by_frames."""
    for max_frames, max_images in card["requires_json"]["limits"]["ref_images_by_frames"]:
        if frames <= max_frames:
            return max_images
    raise TemplateError(f"length {frames} is above every ref_images_by_frames bucket")


def build_payload(graph, card, values):
    inputs = card["inputs"]
    unknown = set(values) - set(inputs)
    if unknown:
        raise TemplateError(f"unknown inputs: {sorted(unknown)}")
    g = copy.deepcopy(graph)
    vals = {}
    for name, spec in inputs.items():
        if name in values:
            vals[name] = values[name]
        elif "default" in spec:
            vals[name] = spec["default"]
        elif spec.get("required", False):
            raise TemplateError(f"missing required input: {name}")
        else:
            continue
        if spec["type"] == "image_list":
            continue
        if spec["type"] == "lora":
            continue
        _check_scalar(name, spec, vals[name])
        for tg in spec["targets"]:
            node = g.get(tg["node"])
            if node is None or tg["field"] not in node["inputs"]:
                raise TemplateError(f"{name}: no {tg['node']}.{tg['field']} in graph")
            v = vals[name]
            node["inputs"][tg["field"]] = tg["template"].format(**{name: v}) if "template" in tg else v
    for name, spec in inputs.items():
        if spec["type"] == "image_list" and name in vals:
            _apply_image_list(g, card, name, spec, vals, values)
        elif spec["type"] == "lora" and name in vals:
            _apply_lora(g, name, spec, vals[name])
    left = [(k, f) for k, n in g.items() for f, x in n["inputs"].items() if isinstance(x, str) and re.search(r"__[A-Z_]+__", x)]
    if left:
        raise TemplateError(f"unsubstituted placeholders left: {left}")
    task_id = vals["task_id"]
    return {"prompt": g, "client_id": task_id, "extra_data": {"studio_task_id": task_id}}


def _apply_image_list(g, card, name, spec, vals, raw):
    imgs = vals[name]
    if not isinstance(imgs, list) or not imgs or not all(isinstance(i, str) and i for i in imgs):
        raise TemplateError(f"{name}: expected a non-empty list of file names")
    if len(imgs) > spec["max"]:
        raise TemplateError(f"{name}: {len(imgs)} images > node max {spec['max']}")
    frames = vals[spec["limit_by"]]
    lim = ref_image_limit(card, frames)
    if len(imgs) > lim:
        raise TemplateError(f"{name}: {len(imgs)} images > limit {lim} at {frames} frames")
    load = spec["load_node"]  # {"id_template": "5_{i}", "proto": "5_0"}
    proto = copy.deepcopy(g[load["proto"]])  # the shipped graph holds exactly one ref (5_0); nodes 5_1.. are cloned from it
    tgt = spec["target"]
    for i, fn in enumerate(imgs):
        n = copy.deepcopy(proto)
        n["inputs"][load["field"]] = fn
        nid = load["id_template"].format(i=i)
        g[nid] = n
        g[tgt["node"]]["inputs"][tgt["field_template"].format(i=i)] = [nid, 0]


def _apply_lora(g, name, spec, v):
    if not isinstance(v, dict) or not isinstance(v.get("name"), str) or not v["name"]:
        raise TemplateError(f"{name}: expected {{'name': str, 'strength': float}}")
    s = v.get("strength", 1.0)
    if isinstance(s, bool) or not isinstance(s, (int, float)) or not -4 <= s <= 4:
        raise TemplateError(f"{name}: strength {s!r} out of [-4, 4]")
    nid = spec["node_id"]
    g[nid] = {"class_type": "LoraLoaderModelOnly",
              "inputs": {"model": list(spec["after"]), "lora_name": v["name"], "strength_model": s}}
    for r in spec["rewire"]:
        g[r["node"]]["inputs"][r["field"]] = [nid, 0]


def main(argv):
    card_path, values_path = argv[1], argv[2]
    card = json.load(open(card_path))
    graph_path = argv[3] if len(argv) > 3 else os.path.join(os.path.dirname(os.path.abspath(card_path)), card["graph"])
    payload = build_payload(json.load(open(graph_path)), card, json.load(open(values_path)))
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main(sys.argv)
