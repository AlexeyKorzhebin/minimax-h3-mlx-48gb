"""The Krea 2 keyframe of a scene (spec §9.1): the project's style, the verbatim portrait of each
hero on screen, the scene's first moment (`state_in`) and a fixed framing — no sound. Size by the
frame format, sides a multiple of 64 (1344×768 measured, GPU-DAY-20261020 п.2)."""
from __future__ import annotations

SIZES = {"16:9": (1344, 768), "9:16": (768, 1344)}
DEFAULT_STYLE = "Live-action photograph, natural light, natural colors."
FRAMING = "Medium shot at eye level, the face fully visible, sharp focus on the face."


def _sentence(text: str) -> str:
    text = " ".join(text.split()).strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    for mark in (". ", "! ", "? "):
        if mark in text:
            return text[:text.index(mark) + 1]
    return text


def keyframe_prompt(*, style_block: str, portrait_phrases: list[str], state_in: str,
                    action_text: str) -> str:
    """`state_in` is the scene's first moment; without it, the first sentence of the action."""
    moment = state_in.strip() or _first_sentence(action_text)
    parts = [_sentence(style_block) or DEFAULT_STYLE,
             *(_sentence(p) for p in portrait_phrases), _sentence(moment), FRAMING]
    return " ".join(p for p in parts if p)


def keyframe_size(frame_format: str) -> tuple[int, int]:
    return SIZES[frame_format]
