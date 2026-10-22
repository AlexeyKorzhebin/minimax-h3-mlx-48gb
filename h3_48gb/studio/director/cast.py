"""Cast card: Russian description -> English description + portrait phrase (spec §7 O5, §9.1)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from h3_48gb.studio.errors import StudioError
from h3_48gb.studio.llm import Llm, validate_reply

PROMPT = Path(__file__).parent / "prompts" / "cast.md"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
MAX_PORTRAIT_WORDS = 30

CAST_SCHEMA = {
    "name": "studio_cast_card",
    "schema": {
        "type": "object",
        "properties": {"description_en": {"type": "string"},
                       "portrait_phrase": {"type": "string"}},
        "required": ["description_en", "portrait_phrase"],
        "additionalProperties": False,
    },
}


def check_card(description_en: str, portrait_phrase: str) -> None:
    """The same checks for a model's card and for one typed by hand."""
    for name, text in (("description_en", description_en), ("portrait_phrase", portrait_phrase)):
        if not text.strip():
            raise StudioError(400, "cast_card_invalid", f"Пустое поле {name}", {"field": name})
        if _CYRILLIC.search(text):
            raise StudioError(400, "cast_card_invalid", f"Кириллица в {name}: нужен английский",
                              {"field": name})
    if len(portrait_phrase.split()) > MAX_PORTRAIT_WORDS:
        raise StudioError(400, "cast_card_invalid",
                          f"Портретная фраза длиннее {MAX_PORTRAIT_WORDS} слов",
                          {"field": "portrait_phrase"})


def describe(llm: Llm, *, tag: str, description_ru: str) -> dict:
    messages = [{"role": "system", "content": PROMPT.read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps({"tag": tag,
                                                        "description_ru": description_ru},
                                                       ensure_ascii=False)}]
    card = llm.json_turn(messages, CAST_SCHEMA,
                         "Ответ строго одним JSON-объектом {description_en, portrait_phrase}.")
    validate_reply(card, CAST_SCHEMA)
    description_en = card["description_en"].strip()
    portrait_phrase = card["portrait_phrase"].strip().rstrip(". ")
    try:
        check_card(description_en, portrait_phrase)
    except StudioError as err:
        raise StudioError(502, "llm_failed", f"Карточка героя от модели не годится: {err.message}",
                          err.detail) from err
    return {"description_en": description_en, "portrait_phrase": portrait_phrase}
