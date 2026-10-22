"""ComfyUI templates of the studio (spec §8.2, §10 L7b): the package copies equal the source of
truth in docs/h3-modes/comfy-templates/, and every card has the shape the adapter relies on."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from h3_48gb.studio import templates
from h3_48gb.studio.executors.comfy_template import TemplateError

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "h3-modes" / "comfy-templates"
PACKAGE = ROOT / "h3_48gb" / "studio" / "comfy_templates"
FILES = ["h3-fl2va.card.json", "h3-fl2va.graph.json", "h3-ref2va.card.json",
         "h3-ref2va.graph.json", "krea2-t2i.card.json", "krea2-t2i.graph.json"]


def test_the_package_holds_exact_copies_of_the_source_of_truth():
    assert sorted(p.name for p in PACKAGE.iterdir()) == FILES
    for name in FILES:
        assert (PACKAGE / name).read_bytes() == (SOURCE / name).read_bytes(), name
    assert (ROOT / "h3_48gb/studio/executors/comfy_template.py").read_bytes() == \
        (SOURCE / "comfy_template.py").read_bytes()


@pytest.mark.parametrize("template_id", ["krea2-t2i", "h3-fl2va", "h3-ref2va"])
def test_every_card_has_the_shape_the_adapter_reads(template_id):
    t = templates.load(template_id)
    card, graph = t.card, t.graph
    assert card["id"] == template_id and card["executor"] == "comfyui"
    classes = {n["class_type"] for n in graph.values()}
    assert sorted(card["requires_json"]["class_types"]) == sorted(classes)
    assert sorted(card["requires_json"]["models"]) == sorted(
        {"diffusion_models", "text_encoders", "vae"})
    assert isinstance(card["requires_json"]["estimate_s"], (int, float))
    out = graph[card["output"]["node"]]
    assert (out["class_type"], card["output"]["kind"]) in (("SaveImage", "image"),
                                                           ("SaveVideo", "video"))
    assert out["inputs"]["filename_prefix"] == "studio/__TASK_ID__/f"
    for name, spec in card["inputs"].items():
        for target in spec.get("targets", []):
            assert target["field"] in graph[target["node"]]["inputs"], (name, target)
    assert card["inputs"]["task_id"]["targets"] == [
        {"node": card["output"]["node"], "field": "filename_prefix",
         "template": "studio/{task_id}/f"}]


def test_the_h3_facts_the_studio_plans_with():
    """GPU-DAY-20261021 п.2–3 and the owner's product limit (22.10): 6 pictures up to 345
    frames; head drop 1 for fl2va only."""
    fl2va, ref2va, krea = (templates.load(t) for t in ("h3-fl2va", "h3-ref2va", "krea2-t2i"))
    for t in (fl2va, ref2va):
        assert t.requires["limits"]["ref_images_by_frames"] == [[345, 6]]
        assert t.frame_bounds() == (73, 345)
        assert (t.ref_image_limit(73), t.ref_image_limit(345)) == (6, 6)
        with pytest.raises(TemplateError, match="above every ref_images_by_frames bucket"):
            t.ref_image_limit(362)
    assert (fl2va.head_drop_frames, ref2va.head_drop_frames, krea.head_drop_frames) == (1, 0, 0)
    assert (fl2va.kind, ref2va.kind, krea.kind) == ("video", "video", "image")
    assert (krea.estimate_s, fl2va.estimate_s, ref2va.estimate_s) == (6.0, 90.0, 100.0)
    assert krea.frame_bounds() is None


def test_the_sha256_changes_with_the_graph(tmp_path, monkeypatch):
    before = templates.load("krea2-t2i").sha256
    for name in ("krea2-t2i.card.json", "krea2-t2i.graph.json"):
        (tmp_path / name).write_bytes((PACKAGE / name).read_bytes())
    graph = json.loads((tmp_path / "krea2-t2i.graph.json").read_text())
    graph["7"]["inputs"]["cfg"] = 2.0
    (tmp_path / "krea2-t2i.graph.json").write_text(json.dumps(graph))
    monkeypatch.setattr(templates, "TEMPLATE_DIR", tmp_path)
    templates.load.cache_clear()
    try:
        assert templates.load("krea2-t2i").sha256 != before
    finally:
        templates.load.cache_clear()


def test_an_unknown_template_is_refused():
    with pytest.raises(templates.UnknownTemplate):
        templates.load("sd-turbo")


def test_mode_routing_constants():
    assert templates.IDS == ("krea2-t2i", "h3-fl2va", "h3-ref2va")
    assert templates.TEMPLATE_FOR_MODE == {"from_frame": "h3-fl2va", "refs": "h3-ref2va"}
    assert templates.IMAGE_TEMPLATE == "krea2-t2i"


def test_an_absent_lora_removes_the_node_and_leaves_the_wiring_alone():
    """Spec rev 3.2: the LoRA node is optional. `lora: None` means no node and no rewiring — not
    a substituted null (the shipped adapter would refuse it as a malformed LoRA)."""
    t = templates.load("krea2-t2i")
    base = {"task_id": "T1", "prompt": "a cat", "seed": 7, "width": 768, "height": 1344}
    plain = t.build_payload(base)
    nulled = t.build_payload({**base, "lora": None})
    assert nulled == plain
    assert "lora" not in nulled["prompt"]
    assert nulled["prompt"]["7"]["inputs"]["model"] == t.graph["7"]["inputs"]["model"]
    with_lora = t.build_payload({**base, "lora": {"name": "x.safetensors", "strength": 0.5}})
    assert with_lora["prompt"]["lora"]["inputs"] == {
        "model": ["1", 0], "lora_name": "x.safetensors", "strength_model": 0.5}
    assert with_lora["prompt"]["7"]["inputs"]["model"] == ["lora", 0]
