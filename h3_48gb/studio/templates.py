"""ComfyUI workflow templates of the studio (spec §8.2, §10 L7b): `comfy_templates/<id>.graph.json`
(API-format graph with placeholders) + `<id>.card.json` (requires, limits, inputs, head drop).
The files are copies of `docs/h3-modes/comfy-templates/` — the source of truth; a test keeps the
copies byte-equal. Substitution is `executors/comfy_template.build_payload` (same source)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from h3_48gb.studio.executors import comfy_template

TEMPLATE_DIR = Path(__file__).parent / "comfy_templates"
IDS = ("krea2-t2i", "h3-fl2va", "h3-ref2va")
#: Which template renders a scene of each mode (§8.2; `text` has no template in part 1).
TEMPLATE_FOR_MODE = {"from_frame": "h3-fl2va", "refs": "h3-ref2va"}
#: The keyframe of step 4 (§9.1) is a Krea 2 picture. Revision 4 (spec §14 seam 1): named for what
#: it is — S2b brings more picture templates, and the keyframe stays on this one.
KEYFRAME_TEMPLATE = "krea2-t2i"


class UnknownTemplate(KeyError):
    pass


@dataclass(frozen=True)
class Template:
    id: str
    card: dict
    graph: dict
    sha256: str          # of the card and graph files, in that order — goes into inputs_json

    @property
    def requires(self) -> dict:
        return self.card["requires_json"]

    @property
    def kind(self) -> str:             # "image" | "video"
        return self.card["output"]["kind"]

    @property
    def estimate_s(self) -> float:
        return float(self.requires["estimate_s"])

    @property
    def head_drop_frames(self) -> int:
        return int(self.card.get("head_drop_frames", 0))

    def frame_bounds(self) -> tuple[int, int] | None:
        frames = self.requires["limits"].get("frames")
        return (frames[0], frames[1]) if frames else None

    def ref_image_limit(self, frames: int) -> int:
        """L7b: the most reference pictures a clip of `frames` frames may carry."""
        return comfy_template.ref_image_limit(self.card, frames)

    def build_payload(self, values: dict) -> dict:
        # Spec rev 3.2: the LoRA node is optional. `lora: None` means "no LoRA" — drop the key so
        # the graph keeps its LoRA-free wiring, instead of handing the adapter a null to refuse.
        values = {k: v for k, v in values.items() if not (k == "lora" and v is None)}
        return comfy_template.build_payload(self.graph, self.card, values)


@lru_cache(maxsize=None)
def load(template_id: str) -> Template:
    if template_id not in IDS:
        raise UnknownTemplate(template_id)
    card_bytes = (TEMPLATE_DIR / f"{template_id}.card.json").read_bytes()
    card = json.loads(card_bytes)
    graph_bytes = (TEMPLATE_DIR / card["graph"]).read_bytes()
    return Template(id=template_id, card=card, graph=json.loads(graph_bytes),
                    sha256=hashlib.sha256(card_bytes + graph_bytes).hexdigest())
