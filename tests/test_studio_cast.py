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


# --- review round 1 (task 9) ------------------------------------------------------------------

import sqlite3
import struct
import zlib

import pytest

from h3_48gb.studio.llm import ProviderLlm

MAX_PIXELS = 50_000_000


def _mono_png(width: int, height: int) -> bytes:
    """A valid 1-bit PNG of any size, a few hundred KB at most: all-zero rows deflate to nothing."""
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    row = b"\x00" * (1 + (width + 7) // 8)
    packer = zlib.compressobj(9)
    idat = b"".join(packer.compress(row) for _ in range(height)) + packer.flush()
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "out" / "studio" / "studio.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def _cast(client, pid, revision=1, **card):
    body = {"expected_revision": revision, "tag": "anna", "description_ru": "ж", **card}
    return client.post(f"/api/studio/projects/{pid}/cast", json=body)


def test_a_picture_over_50_megapixels_is_413_and_exactly_50_is_taken(tmp_path):
    """Review I1: a 680 KB file that decodes to 700 MB must not reach `load()`."""
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    assert _upload(client, data=_mono_png(10000, 5000)).status_code == 201
    r = _upload(client, data=_mono_png(16666667, 3))
    assert r.status_code == 413
    assert r.json() == {"error": {"code": "asset_pixels_too_large",
                                  "message": "Картинка слишком большая по числу пикселей",
                                  "detail": {"pixels": 50_000_001, "max": MAX_PIXELS}}}
    assert len(list((tmp_path / "out" / "studio" / "assets").glob("*"))) == 1   # only the 50 MP one


def test_pillows_own_bomb_error_is_413_too(tmp_path):
    """Above 2x Pillow's default limit Image.open itself refuses; that is a size refusal, not
    a broken file."""
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    r = _upload(client, data=_mono_png(200_000_000 // 3 + 1, 3))
    assert (r.status_code, r.json()["error"]["code"]) == (413, "asset_pixels_too_large")


def test_a_file_of_exactly_25_mb_is_taken(tmp_path):
    from h3_48gb.studio.assets import MAX_ASSET_BYTES
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    r = _upload(client, data=TINY_PNG + b"\x00" * (MAX_ASSET_BYTES - len(TINY_PNG)))
    assert (r.status_code, r.json()["bytes"]) == (201, MAX_ASSET_BYTES)


def test_the_same_asset_twice_in_one_card_is_400_not_500(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    a = _upload(client).json()["id"]
    r = _cast(client, p["id"], description_en="A woman.", portrait_phrase="a woman",
              assets=[{"asset_id": a, "role": "image"}, {"asset_id": a, "role": "image"}])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "args_invalid"
    assert r.json()["error"]["detail"]["errors"][0]["loc"] == ["body", "assets"]


def test_a_manual_phrase_is_normalised_before_it_is_checked(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    r = _cast(client, p["id"], description_en="A woman.", portrait_phrase="...")
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["detail"]) == \
        (400, "cast_card_invalid", {"field": "portrait_phrase"})
    r = _cast(client, p["id"], description_en="  A woman.  ", portrait_phrase=" a woman. ")
    card = r.json()["cast_member"]
    assert (card["description_en"], card["portrait_phrase"]) == ("A woman.", "a woman")


@pytest.mark.parametrize("given, missing", [({"description_en": "A woman."}, "portrait_phrase"),
                                            ({"portrait_phrase": "a woman"}, "description_en")])
def test_half_a_manual_card_is_refused_not_sent_to_the_llm(tmp_path, given, missing):
    llm = ScriptedLlm([])
    client = make_client(tmp_path, llm=llm)
    p = _project(client)
    r = _cast(client, p["id"], **given)
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["detail"]) == \
        (400, "cast_card_invalid", {"field": missing})
    assert llm.calls == []


def test_lowercase_and_yo_cyrillic_are_refused_in_both_fields(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    for en, phrase, field in (("a женщина", "a woman", "description_en"),
                              ("A woman.", "a ёж", "portrait_phrase"),
                              ("A Ёж.", "a woman", "description_en")):
        r = _cast(client, p["id"], description_en=en, portrait_phrase=phrase)
        assert (r.status_code, r.json()["error"]["detail"]) == (400, {"field": field}), (en, phrase)


def test_portrait_phrase_limit_is_30_words(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    r = _cast(client, p["id"], description_en="A woman.", portrait_phrase=" ".join(["word"] * 31))
    assert (r.status_code, r.json()["error"]["detail"]) == (400, {"field": "portrait_phrase"})
    r = _cast(client, p["id"], description_en="A woman.", portrait_phrase=" ".join(["word"] * 30))
    assert r.status_code == 201


def test_a_stale_revision_is_refused_before_the_llm_is_asked(tmp_path):
    llm = ScriptedLlm([])
    client = make_client(tmp_path, llm=llm)
    p = _project(client)
    r = _cast(client, p["id"], revision=7)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "revision_conflict")
    assert llm.calls == []


def test_an_edit_during_the_llm_call_is_a_409_and_writes_nothing(tmp_path):
    """The model takes minutes; the project can move meanwhile (И5)."""
    box = {}

    class EditingLlm:
        label = "editing"

        def json_turn(self, messages, schema, retry_reminder):
            box["patch"] = box["client"].patch(f"/api/studio/projects/{box['pid']}", json={
                "expected_revision": 1, "title": "Другой"})
            return {"description_en": "A woman.", "portrait_phrase": "a woman"}

    client = make_client(tmp_path, llm=EditingLlm())
    box.update(client=client, pid=_project(client)["id"])
    r = _cast(client, box["pid"])
    assert box["patch"].status_code == 200
    assert (r.status_code, r.json()["error"]["code"]) == (409, "revision_conflict")
    assert client.get(f"/api/studio/projects/{box['pid']}").json()["cast"] == []


def test_the_journal_bodies_of_asset_and_cast(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    a = _upload(client).json()
    _cast(client, p["id"], description_en="A woman.", portrait_phrase="a woman")
    rows = [(r["kind"], r["project_id"], r["payload_json"]) for r in _db(tmp_path).execute(
        "SELECT kind, project_id, payload_json FROM event WHERE kind IN "
        "('asset_imported', 'cast_added') ORDER BY seq")]
    assert rows == [("asset_imported", None, json.dumps({"asset_id": a["id"], "kind": "image"},
                                                         sort_keys=True)),
                    ("cast_added", p["id"], '{"tag": "anna"}')]


def test_a_model_card_in_cyrillic_is_a_clear_502(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([
        {"description_en": "Женщина с косой.", "portrait_phrase": "a woman"}]))
    p = _project(client)
    r = _cast(client, p["id"])
    assert r.status_code == 502
    assert r.json() == {"error": {
        "code": "llm_failed",
        "message": "Карточка героя от модели не годится: Кириллица в description_en: нужен английский",
        "detail": {"field": "description_en"}}}
    assert client.get(f"/api/studio/projects/{p['id']}").json()["cast"] == []


def test_a_blank_russian_description_is_refused(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    r = _cast(client, p["id"], description_ru="   ", description_en="A woman.",
              portrait_phrase="a woman")
    assert (r.status_code, r.json()["error"]["code"]) == (400, "args_invalid")
    assert r.json()["error"]["detail"]["errors"][0]["loc"] == ["body", "description_ru"]


def test_a_failed_database_write_removes_the_stored_file(tmp_path, monkeypatch):
    from h3_48gb.studio import assets as assets_module

    def boom(*args, **kwargs):
        raise StudioError(503, "disk_gone", "x")
    monkeypatch.setattr(assets_module.events, "emit", boom)
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    r = _upload(client)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "disk_gone")
    assert list((tmp_path / "out" / "studio" / "assets").glob("*")) == []
    assert _db(tmp_path).execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 0


def test_the_asset_path_is_stored_relative_to_the_studio_home(tmp_path):
    """Moving `<outdir>` must not orphan every picture."""
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    a = _upload(client).json()
    stored = _db(tmp_path).execute("SELECT path FROM asset WHERE id = ?", (a["id"],)).fetchone()[0]
    assert stored == f"assets/{a['id']}.png"
    assert client.get(a["url"]).content == TINY_PNG


def test_cast_without_a_configured_provider_is_503(tmp_path):
    client = make_client(tmp_path)
    p = _project(client)
    r = _cast(client, p["id"])
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "llm_not_configured"
    assert r.json()["error"]["message"] == "Сценарист не настроен: нет провайдера в studio/providers.json"
    assert r.json()["error"]["detail"] == {"provider": None}


def test_cast_while_the_render_holds_the_gpu_is_409(tmp_path):
    import fcntl
    import os
    home = tmp_path / "out" / "studio"
    home.mkdir(parents=True)
    (home / "providers.json").write_text(json.dumps({"active": "f", "providers": {"f": {
        "type": "openai", "base_url": "http://127.0.0.1:9", "model": "m",
        "api_key_env": "KEY", "shares_gpu": True}}}))
    (home / ".env").write_text("KEY=synthetic\n")
    client = make_client(tmp_path, llm=ProviderLlm(home, home / "gpu-mac.lock", lock_timeout_s=0.2))
    p = _project(client)
    fd = os.open(home / "gpu-mac.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = _cast(client, p["id"])
    finally:
        os.close(fd)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "gpu_busy")
    assert r.json()["error"]["message"] == "Сценарист ждёт видеокарту: её занял рендер"
    assert r.json()["error"]["detail"] == {"lock": str(home / "gpu-mac.lock")}


# --- review round 2 (task 9) ------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_between_pillows_warning_and_error_the_refusal_still_names_the_pixels(tmp_path):
    """100 MP is past Pillow's warning threshold (89 MP): the studio's own check answers, with the
    exact count, and no process-wide warning filter is involved."""
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    r = _upload(client, data=_mono_png(10000, 10000))
    assert (r.status_code, r.json()["error"]["detail"]) == (
        413, {"pixels": 100_000_000, "max": MAX_PIXELS})


def test_a_row_without_its_file_is_404_asset_not_found(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    a = _upload(client).json()
    (tmp_path / "out" / "studio" / "assets" / f"{a['id']}.png").unlink()
    r = client.get(a["url"])
    assert (r.status_code, r.json()) == (404, {"error": {
        "code": "asset_not_found", "message": "Файл не найден", "detail": {"asset_id": a["id"]}}})


def test_the_phrase_is_stripped_again_after_its_trailing_dots(tmp_path):
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    p = _project(client)
    r = _cast(client, p["id"], description_en="A woman.", portrait_phrase="a woman. .")
    assert r.json()["cast_member"]["portrait_phrase"] == "a woman"


def test_a_multi_frame_jpeg_from_a_phone_is_taken_as_jpg(tmp_path):
    """Pillow reports a JPEG with an MPF block as `MPO`, not `JPEG`."""
    from PIL import Image
    buf = io.BytesIO()
    frame = Image.new("RGB", (4, 4), (200, 10, 10))
    frame.save(buf, "MPO", save_all=True, append_images=[Image.new("RGB", (4, 4))])
    data = buf.getvalue()
    with Image.open(io.BytesIO(data)) as probe:
        assert probe.format == "MPO"
    client = make_client(tmp_path, llm=ScriptedLlm([]))
    r = _upload(client, data=data, name="IMG.jpg")
    assert (r.status_code, r.json()["mime"]) == (201, "image/jpeg")
