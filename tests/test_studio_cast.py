"""Slice S1 step 2: upload a hero picture (O6, И7) and add the hero to the cast (O5)."""
from __future__ import annotations

import io
import json

from _studio_fixtures import TINY_PNG, ScriptedLlm, make_client

from h3_48gb.studio.errors import StudioError

# Literals, not imports from the code under test (review r2, Н2).
CAST_PROMPT = (
    'You write the English card of one character for the MiniMax H3 video model.\n'
    '\n'
    'Input: a JSON object with `tag` (a short latin name) and `description_ru` (how the owner\n'
    'describes the character, in Russian).\n'
    '\n'
    'Answer with one JSON object:\n'
    '- `description_en` — the same character in English, 1–3 sentences: age, build, face, hair,\n'
    '  clothing, one or two distinctive details. Concrete and visual. No names of real people, no\n'
    '  mood words, no Cyrillic.\n'
    '- `portrait_phrase` — one sentence of at most 30 words that identifies the character on screen\n'
    '  (for example "a woman in her thirties with a loose dark braid and a long grey linen dress").\n'
    '  It will be pasted verbatim into every scene where the character appears, so it must read as\n'
    '  a noun phrase that can start a sentence.\n'
)
CAST_SCHEMA = {"name": "studio_cast_card", "schema": {
    "type": "object",
    "properties": {"description_en": {"type": "string"}, "portrait_phrase": {"type": "string"}},
    "required": ["description_en", "portrait_phrase"],
    "additionalProperties": False}}

PNG_SHA256 = "d2c8571330616ad74a60d54379918d803dca8083d62e9fac6c41e619c0ce1d4b"


def _project(client):
    return client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"}).json()


def _upload(client, data=TINY_PNG, name="hero.png", kind=None):
    form = {"kind": kind} if kind else {}
    return client.post("/api/studio/assets", files={"file": (name, io.BytesIO(data))}, data=form)


def test_upload_stores_the_bytes_once_and_serves_them(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    r = _upload(client)
    assert r.status_code == 201
    asset = r.json()
    assert asset == {"id": asset["id"], "kind": "image", "sha256": PNG_SHA256, "bytes": 75,
                     "mime": "image/png", "url": f"/api/studio/assets/{asset['id']}/file"}
    served = client.get(asset["url"])
    assert (served.content, served.headers["content-type"]) == (TINY_PNG, "image/png")
    second = _upload(client).json()
    assert second["id"] != asset["id"]
    assert client.get(asset["url"]).content == TINY_PNG


def test_upload_refuses_what_is_not_a_picture(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    r = _upload(client, data=b"%PDF-1.7 ...", name="x.pdf")
    assert (r.status_code, r.json()["error"]["code"]) == (400, "asset_type_invalid")
    r = _upload(client, kind="audio")
    assert (r.status_code, r.json()["error"]["detail"]) == (400, {"sniffed": "image",
                                                                  "kind": "audio"})


def test_a_right_signature_on_broken_content_is_refused(tmp_path):
    """Review finding 7: signature + zeros, and a real PNG cut in half, are not pictures."""
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    for data in (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, TINY_PNG[:50]):
        r = _upload(client, data=data)
        assert r.json() == {"error": {"code": "asset_corrupt",
                                      "message": "Картинка повреждена или не читается",
                                      "detail": {}}}
        assert r.status_code == 400
    assert list((tmp_path / "out" / "studio" / "assets").glob("*")) == []


def test_upload_over_25_mb_is_413(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    r = _upload(client, data=TINY_PNG + b"\x00" * (25 * 1024 * 1024))
    assert (r.status_code, r.json()["error"]["code"]) == (413, "asset_too_large")


def test_an_iphone_heic_photo_is_refused_with_the_formats_named(tmp_path):
    """Review Focus 1: the owner's phone shoots HEIC."""
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 32
    r = _upload(client, data=heic, name="IMG_0001.HEIC")
    assert r.status_code == 400
    assert r.json()["error"] == {"code": "asset_type_invalid",
                                 "message": "Нужна картинка PNG, JPEG или WebP", "detail": {}}


def test_cast_card_from_the_llm(tmp_path):
    llm = ScriptedLlm([{"description_en": "A woman in her thirties, dark braid, grey linen dress.",
                        "portrait_phrase": "a woman in her thirties with a loose dark braid."}])
    client = make_client(tmp_path, llm=llm)
    p = _project(client)
    asset = _upload(client).json()
    r = client.post(f"/api/studio/projects/{p['id']}/cast", json={
        "expected_revision": 1, "tag": "anna", "description_ru": "женщина лет тридцати, коса",
        "assets": [{"asset_id": asset["id"], "role": "image"}]})
    assert r.status_code == 201
    body = r.json()
    assert body["project"]["revision"] == 2
    assert body["cast_member"] == {
        "id": body["cast_member"]["id"], "tag": "anna",
        "description_ru": "женщина лет тридцати, коса",
        "description_en": "A woman in her thirties, dark braid, grey linen dress.",
        "portrait_phrase": "a woman in her thirties with a loose dark braid",
        "assets": [{"asset_id": asset["id"], "role": "image", "ord": 1, "url": asset["url"]}]}
    (call,) = llm.calls
    assert call["schema"] == CAST_SCHEMA
    assert call["messages"] == [
        {"role": "system", "content": CAST_PROMPT},
        {"role": "user", "content": json.dumps({"tag": "anna",
                                                "description_ru": "женщина лет тридцати, коса"},
                                               ensure_ascii=False)}]
    assert client.get(f"/api/studio/projects/{p['id']}").json()["cast"] == [body["cast_member"]]


def test_a_cast_reply_off_the_schema_is_502_not_500(tmp_path):
    """Review finding 6: a json_schema route may hand back anything; the studio checks it."""
    for reply, path in (({}, "/"), ({"description_en": "A woman.", "portrait_phrase": 7},
                                    "/portrait_phrase")):
        client = make_client(tmp_path / path.strip("/").replace("_", ""), llm=ScriptedLlm([reply]))
        p = _project(client)
        r = client.post(f"/api/studio/projects/{p['id']}/cast", json={
            "expected_revision": 1, "tag": "anna", "description_ru": "ж"})
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "llm_failed"
        assert r.json()["error"]["message"] == "Модель ответила не по схеме"
        assert r.json()["error"]["detail"]["path"] == path


def test_manual_card_skips_the_llm(tmp_path):
    llm = ScriptedLlm([])
    client = make_client(tmp_path, llm=llm)
    p = _project(client)
    r = client.post(f"/api/studio/projects/{p['id']}/cast", json={
        "expected_revision": 1, "tag": "anna", "description_ru": "женщина",
        "description_en": "A woman.", "portrait_phrase": "a woman with a braid"})
    assert r.status_code == 201
    assert (r.json()["cast_member"]["description_en"], llm.calls) == ("A woman.", [])


def test_cyrillic_in_a_manual_card_is_refused(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    r = client.post(f"/api/studio/projects/{p['id']}/cast", json={
        "expected_revision": 1, "tag": "anna", "description_ru": "женщина",
        "description_en": "Женщина.", "portrait_phrase": "a woman"})
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["detail"]) == \
        (400, "cast_card_invalid", {"field": "description_en"})


def test_llm_failure_is_502_and_nothing_is_written(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([StudioError(502, "llm_failed", "down")]))
    p = _project(client)
    r = client.post(f"/api/studio/projects/{p['id']}/cast", json={
        "expected_revision": 1, "tag": "anna", "description_ru": "женщина"})
    assert (r.status_code, r.json()["error"]["code"]) == (502, "llm_failed")
    snap = client.get(f"/api/studio/projects/{p['id']}").json()
    assert (snap["cast"], snap["project"]["revision"], snap["last_seq"]) == ([], 1, 1)


def test_same_tag_twice_is_409_and_stale_revision_is_409(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    card = {"tag": "anna", "description_ru": "ж", "description_en": "A woman.",
            "portrait_phrase": "a woman"}
    assert client.post(f"/api/studio/projects/{p['id']}/cast",
                       json={"expected_revision": 1, **card}).status_code == 201
    stale = client.post(f"/api/studio/projects/{p['id']}/cast", json={"expected_revision": 1, **card})
    assert stale.json()["error"]["code"] == "revision_conflict"
    taken = client.post(f"/api/studio/projects/{p['id']}/cast", json={"expected_revision": 2, **card})
    assert (taken.status_code, taken.json()["error"]["code"]) == (409, "cast_tag_taken")


def test_an_unknown_asset_cannot_be_the_hero_picture(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    r = client.post(f"/api/studio/projects/{p['id']}/cast", json={
        "expected_revision": 1, "tag": "anna", "description_ru": "ж", "description_en": "A woman.",
        "portrait_phrase": "a woman", "assets": [{"asset_id": "a_nope", "role": "image"}]})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "asset_unknown")
    assert client.get(f"/api/studio/projects/{p['id']}").json()["cast"] == []


def test_the_provider_text_does_not_reach_the_api_body_or_the_log(tmp_path, caplog):
    """Review finding 1 end to end: a real ProviderLlm over _FakeLlama, a synthetic key."""
    from _fake_llama import _FakeLlama
    secret = "sk-SYNTHETIC-SECRET-0042"
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": f"oops {secret}"}}]})
    try:
        home = tmp_path / "out" / "studio"
        home.mkdir(parents=True)
        (home / "providers.json").write_text(json.dumps({"active": "f", "providers": {"f": {
            "type": "openai", "base_url": f"http://127.0.0.1:{fake.port}", "model": "m",
            "api_key_env": "KEY", "shares_gpu": False}}}))
        (home / ".env").write_text(f"KEY={secret}\n")
        client = make_client(tmp_path)
        p = _project(client)
        r = client.post(f"/api/studio/projects/{p['id']}/cast", json={
            "expected_revision": 1, "tag": "anna", "description_ru": "ж"})
    finally:
        fake.close()
    assert r.status_code == 502
    assert secret not in r.text
    assert secret not in caplog.text
    events = client.get(f"/api/studio/projects/{p['id']}").json()["last_seq"]
    assert events == 1                          # nothing was journaled for the failed call
