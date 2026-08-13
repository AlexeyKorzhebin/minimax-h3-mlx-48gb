"""Маршруты чата: сессии, ходы, провайдеры, правило «очередь главнее».

**Ни один тест здесь не поднимает настоящий llama-server и не начинает генерацию.** Модель —
`tests/_fake_llama.py` (тот же мок, что в `tests/test_provider.py`), очередь — пустая директория
под `tmp_path`, а «идёт прогон» подменяется на `web._generation_running`: живой прогон стоит
двадцать минут GPU, а проверяемое правило — одна ветка в маршруте.

Три формы повторяются, каждая по причине из `tests/test_web.py`:

* **отказ проверяется по коду, а не по «не 200»** — `409` без `gpu_busy` получается и от сервера,
  который просто не нашёл маршрут;
* **каждое правило проверяется с двух сторон** — «во время прогона локальную модель не поднимаем»
  выполняет и сервер, который вообще ничего не отвечает, поэтому рядом стоит тест о том, что
  внешнему провайдеру тот же прогон не мешает;
* **путь из URL и путь из тела проверяются как пути** — id сессии и путь кадра приходят снаружи и
  превращаются в имя файла, поэтому у обоих есть тест на побег из корня.
"""
import fcntl
import http.client
import json
import os
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import pytest

from _fake_llama import _TURN, _FakeLlama
from h3_48gb import provider
from h3_48gb import queue as q
from h3_48gb import web

#: llama-local без порта: его подставляет `_serve(providers_port=…)`. `/usr/bin/true` вместо
#: настоящего бинаря — если маршрут всё-таки решит запустить процесс, запустится пустышка, а не
#: тридцать гигабайт весов.
_LLAMA = {"type": "llama-local", "llama_server": "/usr/bin/true",
          "presets_ini": "/tmp/presets.ini", "preset": "qwen", "ctx": 4096, "resident_gb": 31}


@dataclass
class _Live:
    """Запущенный сервер и корни, на которые он смотрит, плюс три JSON-хелпера."""

    httpd: object
    port: int
    root: Path          # он же outdir: providers.json, .env и chat/ лежат здесь
    queue_root: Path

    def _request(self, method: str, url: str, payload=None) -> tuple[int, dict]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection = http.client.HTTPConnection(web.LOOPBACK, self.port, timeout=30)
        try:
            connection.request(method, url, body=body, headers=headers)
            response = connection.getresponse()
            content_type = response.getheader("Content-Type")
            raw = response.read()
            status = response.status
        finally:
            connection.close()
        assert content_type == "application/json", (
            f"{method} {url} ответил {content_type!r}; контракт — JSON везде")
        return status, json.loads(raw)

    def get_json(self, url: str) -> dict:
        status, payload = self._request("GET", url)
        assert status == 200, payload
        return payload

    def get_json_raw(self, url: str) -> tuple[int, dict]:
        return self._request("GET", url)

    def post_json(self, url: str, payload: dict) -> dict:
        status, answer = self._request("POST", url, payload)
        assert status == 200, answer
        return answer

    def post_json_raw(self, url: str, payload: dict) -> tuple[int, dict]:
        return self._request("POST", url, payload)

    def delete_json_raw(self, url: str) -> tuple[int, dict]:
        return self._request("DELETE", url)

    def upload_raw(self, data: bytes, filename, *, headers=None) -> tuple[int, dict]:
        """`POST /api/uploads` with a raw body -- the one route on this server that is not JSON,
        so it cannot go through `_request` above (`Content-Type: application/octet-stream`, the
        filename in a header rather than the body).

        `filename` is `quote`d the way `app.js` sends it (`encodeURIComponent`): an HTTP header
        value is plain ASCII, enforced by every browser's `fetch`, so a Cyrillic name has to be
        percent-encoded before it can travel as `X-Filename` at all -- `web.py`'s `_upload_frame`
        `unquote`s it back on arrival. `filename=None` omits `X-Filename` entirely, for the test
        that checks it is required. `headers` overrides/extends the pair this sends by default,
        so a test can still spell `X-Filename` itself if it wants a header this helper does not
        set on its own.
        """
        request_headers = {"Content-Type": "application/octet-stream"}
        if filename is not None:
            request_headers["X-Filename"] = urllib.parse.quote(filename, safe="")
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection(web.LOOPBACK, self.port, timeout=30)
        try:
            connection.request("POST", "/api/uploads", body=data, headers=request_headers)
            response = connection.getresponse()
            content_type = response.getheader("Content-Type")
            raw = response.read()
            status = response.status
        finally:
            connection.close()
        assert content_type == "application/json", (
            f"POST /api/uploads answered {content_type!r}; the contract is JSON everywhere")
        return status, json.loads(raw)


@pytest.fixture
def _serve(tmp_path):
    """Фабрика серверов на временном outdir.

    `_serve()` — один активный llama-local на порту 0 (всегда «down», никто не слушает);
    `_serve(providers_port=N)` — тот же провайдер на порту мока; `_serve(providers=…, active=…)` —
    любая другая роспись; `_serve(roster=False)` — `providers.json` вообще нет.

    Локальный провайдер по умолчанию, а не пустая роспись: почти каждый тест здесь про ход или
    про отказ хода, а с пустой росписью любой из них упирался бы в `provider_unavailable` раньше
    проверяемого правила — и, например, `gpu_busy` был бы «зелёным» никогда не выполняясь.

    Фабрика, а не готовый сервер: `fake_llama` обязан получить порт раньше, чем сервер — свою
    роспись, а `providers.json` пишется до старта.
    """
    started: list[_Live] = []

    def start(providers_port=None, providers=None, active=None, env=None,
              roster=True) -> _Live:
        root = tmp_path / "outdir"
        root.mkdir(exist_ok=True)
        queue_root = q.layout(root / "queue")["root"]
        if providers is None and roster:
            providers = {"qwen-local": {**_LLAMA, "port": providers_port or 0}}
            active = active or "qwen-local"
        if providers is not None:
            (root / "providers.json").write_text(
                json.dumps({"active": active, "providers": providers}), encoding="utf-8")
        if env is not None:
            (root / ".env").write_text(env, encoding="utf-8")
        httpd = web.make_server(queue_root, root, port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        live = _Live(httpd=httpd, port=httpd.server_address[1], root=root, queue_root=queue_root)
        started.append(live)
        return live

    yield start
    for live in started:
        live.httpd.shutdown()
        live.httpd.server_close()


@pytest.fixture
def fake_llama():
    """Мок llama-server, отвечающий одним и тем же корректным ходом."""
    fake = _FakeLlama(chat_payload=_TURN)
    yield fake
    fake.close()


def _external(port: int) -> dict:
    """Роспись с одним внешним провайдером на `port` — тот же протокол, но без процесса."""
    return {"openrouter": {"type": "openai", "base_url": f"http://127.0.0.1:{port}",
                           "model": "m"}}


# -- сессии ------------------------------------------------------------------------------------


def test_chat_session_is_created_and_survives_a_reload(_serve):
    srv = _serve()
    created = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})
    sid = created["id"]
    got = srv.get_json(f"/api/chat/{sid}")
    assert got["source"] == {"kind": "new"}
    assert got["messages"] == []
    assert (Path(srv.root) / "chat" / f"{sid}.json").is_file()


def test_a_session_remembers_the_mode_and_the_keyframe_it_was_opened_with(_serve):
    """Режим и кадр приходят при создании и живут в сессии: ход к модели собирается из них, а не
    из тела хода, поэтому потеря любого из двух видна только здесь."""
    srv = _serve()
    png = Path(srv.root) / "start.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    sid = srv.post_json("/api/chat", {"source": {"kind": "clip", "id": "19-real-run"},
                                      "prompt": "", "mode": "i2v", "image": str(png)})["id"]
    got = srv.get_json(f"/api/chat/{sid}")
    assert got["source"] == {"kind": "clip", "id": "19-real-run"}
    assert got["mode"] == "i2v"
    assert Path(got["image"]) == png.resolve()


def test_a_session_without_a_declared_duration_defaults_to_ten_seconds(_serve):
    """A3: the field's own default (`index.html`'s `#duration` ships with `value="10"`), and the
    number the modal's parse falls back to when nobody has ever set one -- a session opened before
    this field existed, or one whose creator forgot the flag, still reads as a plain ten seconds
    rather than an absent field the model has no line for."""
    srv = _serve()
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    assert srv.get_json(f"/api/chat/{sid}")["duration"] == 10


def test_a_session_opened_from_a_job_carries_the_jobs_declared_duration(_serve):
    """A3, and the same client-side pattern `mode`/`image`/`end_image` already use (T4b):
    `openChatFromJob` reads `--duration` out of the job's own `args` and hands it to this route
    exactly the way it hands `--mode`, so this is the server half of that contract -- whatever
    number arrives with a `source.kind == "job"` creation is the number the session remembers, not
    the form's own `#duration`, which the page never even reads for this path."""
    srv = _serve()
    sid = srv.post_json("/api/chat", {"source": {"kind": "job", "id": "j-9"},
                                      "prompt": "", "duration": 7})["id"]
    assert srv.get_json(f"/api/chat/{sid}")["duration"] == 7


def test_a_declared_duration_that_is_not_a_number_is_refused_the_way_a_bad_mode_is(_serve):
    srv = _serve()
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                                      "duration": "10"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf"), -5, 0, 1e300])
def test_a_declared_duration_outside_the_sane_range_is_refused(_serve, duration):
    """Fix round 1 (review, Important): `duration` reaches a browser as plain `Number(...)`, and
    both halves of that are reachable without any special effort. `Number("-5") || 10` from the
    page's own normalisation keeps `-5` (it is truthy), and Python's `json` module accepts the
    `NaN`/`Infinity`/`-Infinity` tokens as an extension of the standard, so a hand-built request
    -- or a bug in a future caller -- can put any of the six values below on the wire. Before this
    fix `isinstance(value, (int, float))` alone let all of them through, and `duration: nan s`
    reached the model verbatim (`_locked_turn`'s system context) instead of being refused at 400
    the way a bad `mode` already is."""
    srv = _serve()
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                                      "duration": duration})
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), (duration, payload)


@pytest.mark.parametrize("duration", [2.4, 15])
def test_a_declared_duration_inside_the_sane_range_is_accepted(_serve, duration):
    srv = _serve()
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                      "duration": duration})["id"]
    assert srv.get_json(f"/api/chat/{sid}")["duration"] == duration


def test_a_session_remembers_the_end_image_and_mentions_it_in_the_turns_context(_serve,
                                                                                fake_llama):
    """T4b: `openChatFromJob` did not carry `--end-image` into the session, so a chat opened from
    a `flf` job's queue entry lost the last frame the moment it opened -- the model never learned
    it existed and had no way to write the `mode: flf` instruction line correctly. `end_image` is
    allowlisted and stored the same way `image` is (T4b), but never uploaded as a picture: only a
    path mention reaches the system context (`_locked_turn`), because the model needs to know the
    frame exists, not see its bytes on every turn.
    """
    srv = _serve(providers_port=fake_llama.port)
    end = Path(srv.root) / "end.png"
    end.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    sid = srv.post_json("/api/chat", {"source": {"kind": "job", "id": "j-1"}, "prompt": "",
                                      "mode": "flf", "end_image": str(end)})["id"]
    got = srv.get_json(f"/api/chat/{sid}")
    assert Path(got["end_image"]) == end.resolve()

    srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши путь", "prompt": ""})
    request = fake_llama.requests[-1]["body"]
    system = request["messages"][0]["content"]
    assert f"end_image: {end.resolve()}" in system
    # no `image` (first frame) was set on this session -- the turn's content must stay plain text,
    # proving `end_image` never becomes an `image_url` payload on its own.
    assert isinstance(request["messages"][-1]["content"], str)


def test_a_source_the_server_does_not_know_is_refused_rather_than_stored(_serve):
    """`kind` — закрытый список: сессия с чужим видом источника молча пережила бы страницу,
    которая её открыть уже не сможет."""
    srv = _serve()
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "whatever"},
                                                      "prompt": ""})
    assert status == 400
    assert payload["error"]["code"] == "args_invalid"
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "job", "sql": "drop"},
                                                      "prompt": ""})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


def test_a_mode_the_generator_does_not_know_is_refused_the_way_a_bad_source_is(_serve):
    """`mode` шёл в сессию не глядя, хотя список закрыт ровно так же, как у `source.kind`.

    Цена молчания разная у двух читателей. Модель получает `## Context\\nmode: <что угодно>` и
    честно пишет промпт под выдуманный режим — вернуть его в очередь нельзя, `--mode` такого не
    примет (`choices` в `cli._add_run_flags`). Страница по `chat.mode` решает, показывать ли
    звуковые секции (`renderChatPrompt`), и на незнакомом режиме тихо считает, что звука нет.
    Оба узнают об ошибке через ход к модели; отказ на создании — единственный момент, когда это
    ещё дёшево.

    Пустой и отсутствующий `mode` остаются валидными: сессию открывают и без режима, а `t2va`
    подставляется на ходу (`DEFAULT_CHAT_MODE`).
    """
    srv = _serve()
    for mode in ("t2vа", "T2VA", "видео", "t2v "):   # первый — с кириллической «а»
        status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "new"},
                                                          "prompt": "", "mode": mode})
        assert status == 400, (mode, payload)
        assert payload["error"]["code"] == "args_invalid", (mode, payload)
        assert sorted(payload["error"]["detail"]["modes"]) == sorted(web.CHAT_MODES), payload
    for mode in sorted(web.CHAT_MODES) + ["", None]:
        answer = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                             "mode": mode})
        assert srv.get_json(f"/api/chat/{answer['id']}")["mode"] == (mode or "")


def test_the_modes_a_session_may_carry_are_the_modes_the_generator_accepts():
    """Два списка, один смысл: разойдясь, они дают либо сессию, которую нельзя поставить в
    очередь, либо отказ странице на режиме, который генератор давно умеет. Список у argparse
    первичен -- он и есть контракт `h3 generate`.
    """
    from h3_48gb import cli
    generate = cli.build_parser()._subparsers._group_actions[0].choices["generate"]
    (mode_flag,) = [a for a in generate._actions if a.dest == "mode"]
    assert set(mode_flag.choices) == set(web.CHAT_MODES), (mode_flag.choices, web.CHAT_MODES)


@pytest.mark.parametrize("sid", ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "sub/dir"])
def test_a_session_id_cannot_climb_out_of_the_chat_directory(_serve, sid):
    """Id сессии — это имя файла: `../../` в нём обязан быть отказом с кодом, а не 404, который
    неотличим от сервера вообще без проверки путей."""
    srv = _serve()
    status, payload = srv.get_json_raw(f"/api/chat/{sid}")
    assert (status, payload["error"]["code"]) == (400, "path_outside_root")
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (400, "path_outside_root")


def test_a_chat_id_too_long_to_be_a_filename_is_a_refusal_not_a_500(_serve):
    """`pathlib` глотает ENOENT, но не ENAMETOOLONG: 400-символьный id в URL — ввод снаружи, и
    отвечать на него «в сервере баг» нечестно. Тот же класс ошибок, что чинит
    `name_too_long_is_a_refusal` для id задачи."""
    srv = _serve()
    sid = "a" * 400
    for status, payload in (srv.get_json_raw(f"/api/chat/{sid}"),
                            srv.post_json_raw(f"/api/chat/{sid}/message",
                                              {"text": "x", "prompt": ""})):
        assert (status, payload["error"]["code"]) == (400, "path_outside_root")


def test_a_message_to_a_session_that_does_not_exist_is_a_named_404(_serve):
    srv = _serve()
    status, payload = srv.get_json_raw("/api/chat/deadbeef")
    assert (status, payload["error"]["code"]) == (404, "chat_not_found")
    status, payload = srv.post_json_raw("/api/chat/deadbeef/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (404, "chat_not_found")


# -- удаление сессии -----------------------------------------------------------------------------


def test_deleting_a_chat_session_removes_the_session_and_its_lock(_serve, fake_llama):
    """`DELETE /api/chat/<id>` — кнопка «очистить» в шапке модалки. Удаляет не только
    `<id>.json`, но и `<id>.lock`: тот переживает каждый ход (`chat_session_lock` его не
    стирает, только снимает замок), и если бы он оставался на диске, а `_chat_dir` со
    временем очистили вручную, второй замок с тем же именем достался бы уже другой сессии.

    Ход перед удалением — не украшение: без него `<id>.lock` ещё не существует
    (`chat_session_lock` создаёт файл замка лениво, только когда кто-то через него проходит),
    и тест ничего не доказал бы про его удаление."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "мрачнее", "prompt": "п"})
    session_path = Path(srv.root) / "chat" / f"{sid}.json"
    lock_path = session_path.with_suffix(".lock")
    assert session_path.is_file() and lock_path.is_file(), "ход должен был оставить оба файла"

    status, answer = srv.delete_json_raw(f"/api/chat/{sid}")
    assert status == 200 and answer == {"ok": True}, answer
    assert not session_path.exists(), "сессия должна быть удалена"
    assert not lock_path.exists(), "замок сессии должен быть удалён вместе с ней"


def test_deleting_a_chat_session_that_does_not_exist_is_a_named_404(_serve):
    srv = _serve()
    status, payload = srv.delete_json_raw("/api/chat/deadbeef")
    assert (status, payload["error"]["code"]) == (404, "chat_not_found"), payload


def test_deleting_a_chat_session_mid_turn_is_refused_and_deletes_nothing(_serve):
    """Если ход уже идёт, замок сессии занят другим потоком (`chat_session_lock`), и удаление
    обязано отказаться тем же кодом, каким отказывается второй ход — `chat_busy`, 409 — а не
    молча выждать и стереть файлы из-под модели, которая их как раз читает и пишет.

    Замок держит `fcntl.flock` из самого теста, напрямую на том же `<id>.lock`, каким его
    открыл бы сервер — тот же приём, каким `chat_session_lock` проверяется в `test_web.py`:
    `flock` различает конкурирующие открытия файла даже в одном процессе, так что настоящий
    ход модели поднимать не нужно."""
    srv = _serve()
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    session_path = Path(srv.root) / "chat" / f"{sid}.json"
    lock_path = session_path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status, payload = srv.delete_json_raw(f"/api/chat/{sid}")
        assert (status, payload["error"]["code"]) == (409, "chat_busy"), payload
        assert session_path.exists(), "занятый замок — ничего не должно быть удалено"
        assert lock_path.exists(), "занятый замок — ничего не должно быть удалено"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# -- очередь главнее ---------------------------------------------------------------------------


def test_a_message_during_a_live_generation_is_refused_and_not_saved(_serve, monkeypatch):
    srv = _serve()
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    # живой прогон: reconcile().alive непусто — подменяем на уровне web-модуля
    monkeypatch.setattr("h3_48gb.web._generation_running", lambda root: {"job": "j-1"})
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message",
                                        {"text": "мрачнее", "prompt": "x"})
    assert status == 409
    assert payload["error"]["code"] == "gpu_busy"
    assert srv.get_json(f"/api/chat/{sid}")["messages"] == []


def test_a_live_generation_does_not_stand_in_the_way_of_an_external_provider(_serve, fake_llama,
                                                                             monkeypatch):
    """Обратная сторона правила: 30 ГБ памяти занимает только локальная модель, поэтому чат через
    openrouter во время прогона обязан работать. Без этого теста «gpu_busy всегда» выглядит как
    правильно реализованное правило."""
    srv = _serve(providers=_external(fake_llama.port), active="openrouter")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    monkeypatch.setattr("h3_48gb.web._generation_running", lambda root: {"job": "j-1"})
    answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "мрачнее", "prompt": "x"})
    assert answer["reply"] == "Сделал мрачнее."


# -- ход ---------------------------------------------------------------------------------------


def test_a_turn_reaches_the_model_with_the_editors_text_not_the_saved_one(_serve, fake_llama):
    """Правка руками: в окне текст A', в сессии сохранён A — модель обязана видеть A'."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "старый"})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "проверь", "prompt": "правленый руками"})
    sent = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "правленый руками" in sent and "старый" not in sent


def test_the_system_message_carries_the_format_doc_and_the_mode(_serve, fake_llama):
    """Модель без документа формата пишет свободный текст, а без режима — инструкцию для i2v в
    чистом t2va-промпте. Оба куска едут в system, и оба проверяются здесь."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "", "mode": "i2v"})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши", "prompt": "п"})
    system = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "integrated_multimodal_description" in system   # это системный промпт Задачи 4
    assert "mode: i2v" in system
    # режим по умолчанию — t2va, а не пустая строка
    other = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{other}/message", {"text": "опиши", "prompt": "п"})
    assert "mode: t2va" in fake_llama.requests[-1]["body"]["messages"][0]["content"]


def test_the_system_message_carries_the_turns_declared_duration(_serve, fake_llama):
    """A3: the modal's «Длительность, с» field lives in the page's own state, not the session it
    was opened with, and it has to reach the model on *every* turn it changes, not only the first
    -- a person may move the number after the session was already open. The session's own default
    (ten, from `_create_chat`) is what the very first turn falls back to when it says nothing."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши", "prompt": "п", "duration": 7})
    system = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "duration: 7 s" in system
    # persisted: the session file itself carries the edited number now, not only this one turn.
    assert srv.get_json(f"/api/chat/{sid}")["duration"] == 7
    # a second turn that says nothing about `duration` keeps the session's own last-known number,
    # not the ten-second default `_create_chat` used when the session was first opened.
    srv.post_json(f"/api/chat/{sid}/message", {"text": "ещё", "prompt": ""})
    assert "duration: 7 s" in fake_llama.requests[-1]["body"]["messages"][0]["content"]


def test_the_history_of_the_session_travels_with_the_next_turn(_serve, fake_llama):
    """Второй ход без первого — это модель с амнезией: «а теперь наоборот» ей не к чему отнести."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "мрачнее", "prompt": "п"})
    srv.post_json(f"/api/chat/{sid}/message", {"text": "а теперь наоборот", "prompt": "п"})
    roles = [m["role"] for m in fake_llama.requests[-1]["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    contents = [m["content"] for m in fake_llama.requests[-1]["body"]["messages"]]
    assert contents[1:] == ["мрачнее", "Сделал мрачнее.", "а теперь наоборот"]


def test_two_turns_of_one_session_at_the_same_time_leave_one_answer_and_no_500(_serve):
    """Две вкладки (или двойной клик) шлют ход одной сессии одновременно.

    Без замка обе читают файл сессии до записи любой из них, обе платят за вызов модели, и
    победитель затирает проигравшего — обмен исчезает молча. Хуже: `write_text_durably` называет
    временный файл по **pid**, а не по потоку, так что два потока одного процесса дерутся за один
    `.tmp-<pid>`, и проигравший получает `FileNotFoundError` → 500 уже *после* оплаченного ответа
    модели. Правильный ответ — честный 409 `chat_busy` **до** обращения к модели.

    Проверяется всё три сразу: ровно один 200 и ровно один 409, ровно один обмен в сессии, ровно
    один запрос к модели. Без последней строчки тест проходил бы и на реализации, которая просто
    выполняет ходы по очереди — а это уже не отказ, а очередь, и страница о ней не знает.
    """
    fake = _FakeLlama(chat_payload=_TURN, delay=0.3)
    try:
        srv = _serve(providers=_external(fake.port), active="openrouter")
        sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
        answers: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def turn():
            got = srv.post_json_raw(f"/api/chat/{sid}/message", {"text": "мрачнее", "prompt": "п"})
            with lock:
                answers.append(got)

        threads = [threading.Thread(target=turn) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert sorted(status for status, _ in answers) == [200, 409], answers
        assert [payload["error"]["code"] for status, payload in answers if status == 409] \
            == ["chat_busy"]
        assert len(srv.get_json(f"/api/chat/{sid}")["messages"]) == 2, "обмен ровно один"
        assert len(fake.requests) == 1, "проигравший не должен был звать модель"
    finally:
        fake.close()


def test_the_reply_and_the_prompt_are_saved_and_survive_a_reload(_serve, fake_llama):
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "мрачнее", "prompt": "п"})
    assert answer["reply"] == "Сделал мрачнее."
    assert answer["prompt"]["overall_soundscape"] == "Wind."
    assert answer["llm"]["status"] == "up"
    saved = srv.get_json(f"/api/chat/{sid}")
    assert [(m["role"], m["content"]) for m in saved["messages"]] == [
        ("user", "мрачнее"), ("assistant", "Сделал мрачнее.")]
    assert saved["prompt_struct"]["non_diegetic_music"] == "N/A"


# -- A4: слаг ------------------------------------------------------------------------------------


def _turn_with_slug(slug):
    """A well-formed turn (the shape `_TURN` fixes) carrying `slug` as given -- whatever type."""
    return {"choices": [{"message": {"content": json.dumps(
        {"reply": "ок", "prompt": None, "slug": slug})}}]}


def test_a_turn_with_a_slug_is_saved_to_the_session_and_returned_to_the_client(_serve):
    fake = _FakeLlama(chat_payload=_turn_with_slug("cat-italian-noon"))
    try:
        srv = _serve(providers_port=fake.port)
        sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
        answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "правь", "prompt": ""})
        assert answer["slug"] == "cat-italian-noon", answer
        assert srv.get_json(f"/api/chat/{sid}")["slug"] == "cat-italian-noon"
    finally:
        fake.close()


def test_a_later_turn_with_no_slug_keeps_the_sessions_last_known_one(_serve):
    """The model does not owe a `slug` on every turn -- only when it hands back a `prompt` at all
    (see the doc paragraph this pins). A turn that answers with `prompt: null` and no `slug` must
    not erase what an earlier turn already named the session.

    One fake server for both turns, mutated in place between them (`_FakeLlama` reads
    `chat_payload` fresh out of its own closure on every request, so mutating the same dict the
    handler already holds a reference to changes what the *next* request gets back, without
    tearing down and re-pointing the roster at a second port mid-session).
    """
    payload = _turn_with_slug("cat-italian-noon")
    fake = _FakeLlama(chat_payload=payload)
    try:
        srv = _serve(providers_port=fake.port)
        sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
        srv.post_json(f"/api/chat/{sid}/message", {"text": "правь", "prompt": ""})
        assert srv.get_json(f"/api/chat/{sid}")["slug"] == "cat-italian-noon"

        payload.clear()
        payload.update({"choices": [{"message": {"content": json.dumps(
            {"reply": "ещё", "prompt": None})}}]})
        answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "ещё раз", "prompt": ""})
        assert answer.get("slug") is None, "этот ход ничего не назвал"
        assert srv.get_json(f"/api/chat/{sid}")["slug"] == "cat-italian-noon", (
            "второй ход без slug не должен был стереть слаг первого")
    finally:
        fake.close()


@pytest.mark.parametrize("slug", [42, True, ["cat", "noon"], {"x": 1}, ""])
def test_a_non_string_or_empty_slug_is_ignored_quietly_not_a_502(_serve, slug):
    """A `slug` that ignores its own type -- a provider outside `response_format`'s reach can send
    anything -- is the same situation `reply`'s own type check exists for (see
    `test_a_reply_that_is_not_a_string_is_refused_instead_of_bricking_the_session`), but the
    resolution is the opposite one, on purpose.

    `reply` is required and structural: it becomes `messages[-1]["content"]`, and a bad type there
    would brick the session the next time `_check_session_shape` reads it back -- so it is a 502.
    `slug` is optional metadata that never reaches `messages` or the session-shape check at all;
    treating a malformed one as "absent" costs nothing and keeps the promise `slug` not being in
    `required` already makes -- its absence, however it came about, is never an error.
    """
    fake = _FakeLlama(chat_payload=_turn_with_slug(slug))
    try:
        srv = _serve(providers_port=fake.port)
        sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
        status, answer = srv.post_json_raw(f"/api/chat/{sid}/message",
                                           {"text": "правь", "prompt": ""})
        assert status == 200, answer
        assert answer.get("slug") is None, answer
        assert "slug" not in srv.get_json(f"/api/chat/{sid}")
    finally:
        fake.close()


# -- кадр --------------------------------------------------------------------------------------


def test_a_session_with_a_keyframe_attaches_it_to_the_turn_as_an_image_part(_serve, fake_llama):
    srv = _serve(providers_port=fake_llama.port)
    png = Path(srv.root) / "start.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n0000")     # содержимое неважно, важна передача
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                      "mode": "i2v", "image": str(png)})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши кадр", "prompt": ""})
    content = fake_llama.requests[-1]["body"]["messages"][-1]["content"]
    kinds = [part["type"] for part in content]
    assert kinds == ["text", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # история сессии хранит только текст
    saved = srv.get_json(f"/api/chat/{sid}")["messages"][-2]
    assert isinstance(saved["content"], str)


def test_a_keyframe_that_stopped_being_readable_is_a_warning_not_a_refusal(_serve, fake_llama):
    """Кадр удалили между открытием сессии и ходом. Ход всё равно должен дойти — просто без
    картинки и с пометкой, которую страница допишет в ленту."""
    srv = _serve(providers_port=fake_llama.port)
    png = Path(srv.root) / "gone.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                      "mode": "i2v", "image": str(png)})["id"]
    png.unlink()
    answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши кадр", "prompt": ""})
    assert answer["reply"] == "Сделал мрачнее."
    assert answer["warning"]["code"] == "image_not_found"
    assert "gone.png" in answer["warning"]["message"]
    assert isinstance(fake_llama.requests[-1]["body"]["messages"][-1]["content"], str)


def test_a_keyframe_that_is_not_an_image_is_refused_when_the_session_opens(_serve):
    """`resolve_within` ограничивает *где* лежит файл, но не *что это за файл*, а `<outdir>/.env`
    лежит внутри корня и хранит токены. Без allowlist типов один POST выгружал его base64-ом
    активному провайдеру — воспроизведено зондом ревью."""
    srv = _serve()
    secret = Path(srv.root) / ".env"
    secret.write_text("OPENROUTER_API_KEY=sk-very-secret\n", encoding="utf-8")
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                                      "mode": "i2v", "image": str(secret)})
    assert (status, payload["error"]["code"]) == (400, "bad_image")


def test_a_session_pointing_at_a_file_that_is_not_an_image_sends_the_turn_without_it(_serve,
                                                                                     fake_llama):
    """Та же проверка второй раз — уже при чтении файла, а не при создании сессии.

    Сессия на диске переживает и правки руками, и версию сервера без проверки на входе, поэтому
    решение «прикладывать ли кадр» принимается там, где читаются байты. Ход при этом не срывается:
    отказывать в разговоре из-за кадра — потерять уже написанный текст.
    """
    srv = _serve(providers_port=fake_llama.port)
    secret = Path(srv.root) / ".env"
    secret.write_text("OPENROUTER_API_KEY=sk-very-secret\n", encoding="utf-8")
    sid = "beefbeef"
    (Path(srv.root) / "chat").mkdir(exist_ok=True)
    (Path(srv.root) / "chat" / f"{sid}.json").write_text(json.dumps(
        {"id": sid, "source": {"kind": "new"}, "mode": "i2v", "image": str(secret),
         "messages": [], "prompt": ""}), encoding="utf-8")
    answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши кадр", "prompt": ""})
    sent = fake_llama.requests[-1]["body"]["messages"][-1]["content"]
    assert isinstance(sent, str), "к ходу приложили не картинку"
    assert "sk-very-secret" not in json.dumps(fake_llama.requests[-1]["body"], ensure_ascii=False)
    assert answer["warning"]["code"] == "bad_image"
    assert ".env" in answer["warning"]["message"]


def test_a_keyframe_bigger_than_the_limit_is_dropped_with_a_warning(_serve, fake_llama,
                                                                    monkeypatch):
    """Потолок размера — не про безопасность, а про то, что 40-мегабайтный кадр в base64 уходит
    в контекст модели и возвращается таймаутом через десять минут."""
    monkeypatch.setattr(web, "CHAT_IMAGE_MAX_BYTES", 8)
    srv = _serve(providers_port=fake_llama.port)
    png = Path(srv.root) / "big.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                      "mode": "i2v", "image": str(png)})["id"]
    answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши кадр", "prompt": ""})
    assert isinstance(fake_llama.requests[-1]["body"]["messages"][-1]["content"], str)
    assert answer["warning"]["code"] == "bad_image"


def test_a_keyframe_outside_every_root_is_refused_when_the_session_opens(_serve, tmp_path):
    """Путь кадра приходит из тела запроса и читается с диска, а байты уезжают внешнему
    провайдеру: без этой проверки `~/.ssh/id_rsa` уходит в openrouter одним POST'ом."""
    srv = _serve()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                                      "mode": "i2v", "image": str(outside)})
    assert (status, payload["error"]["code"]) == (400, "path_outside_root")


# -- A8: кадр приложен ходом (скрепка/dnd в поле чата) -------------------------------------------


def test_a_turn_with_an_image_updates_the_session_and_attaches_the_new_frame(_serve, fake_llama):
    """Кадр, приложенный самим ходом (`image` в теле `message`, задача A8) — не только уходит
    модели этим же ходом (существующий механизм `_turn_content`), но и переживает ход: следующее
    сообщение, которое вовсе не называет кадр, обязано снова увидеть именно его, а не пустоту.
    `set_mode` идёт тем же ходом и должен обновить сохранённый режим сессии ровно так, как если
    бы сессию открыли заново с этим режимом.
    """
    srv = _serve(providers_port=fake_llama.port)
    png = Path(srv.root) / "dropped.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    answer = srv.post_json(f"/api/chat/{sid}/message",
                           {"text": "опиши кадр", "prompt": "", "image": str(png),
                            "set_mode": "i2v"})
    assert answer.get("warning") is None, answer
    sent = fake_llama.requests[-1]["body"]["messages"][-1]["content"]
    kinds = [part["type"] for part in sent]
    assert kinds == ["text", "image_url"]
    assert sent[1]["image_url"]["url"].startswith("data:image/png;base64,")

    saved = srv.get_json(f"/api/chat/{sid}")
    assert Path(saved["image"]) == png.resolve()
    assert saved["mode"] == "i2v"

    # Второй ход ничего не говорит про кадр — сессия обязана помнить прошлый.
    srv.post_json(f"/api/chat/{sid}/message", {"text": "ещё", "prompt": ""})
    second = fake_llama.requests[-1]["body"]["messages"][-1]["content"]
    assert [part["type"] for part in second] == ["text", "image_url"]


def test_an_invalid_image_in_a_turn_is_a_hard_refusal_and_the_session_is_untouched(_serve,
                                                                                   fake_llama):
    """`image` в теле `message` — явное действие человека (скрепка, dnd), не «кадр из прошлого,
    который мог протухнуть» (`_turn_content`'s own warning). Тот же файл, отклонённый как кадр
    при создании сессии (`_chat_image_path`), отклоняется здесь так же жёстко: 400, а не ход,
    отправленный без картинки с оговоркой. И, раз это отказ, ни сессия, ни счётчик обращений к
    модели не должны были измениться.
    """
    srv = _serve(providers_port=fake_llama.port)
    not_an_image = Path(srv.root) / "notes.txt"
    not_an_image.write_text("не кадр", encoding="utf-8")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, payload = srv.post_json_raw(
        f"/api/chat/{sid}/message",
        {"text": "опиши кадр", "prompt": "", "image": str(not_an_image)})
    assert (status, payload["error"]["code"]) == (400, "bad_image"), payload
    saved = srv.get_json(f"/api/chat/{sid}")
    assert saved["image"] == "", "невалидный кадр не должен был попасть в сессию"
    assert saved["messages"] == [], "отказанный ход не должен был лечь в историю"
    assert fake_llama.requests == [], "модель не должна была получить платный вызов на отказе"


def test_a_garbage_set_mode_in_a_turn_is_args_invalid(_serve, fake_llama):
    """`set_mode` — тот же закрытый список, что и `mode` при создании сессии (`CHAT_MODES`), и
    та же причина: мусор здесь становится либо инструкцией для модели, которую она честно
    выполнит, либо решением страницы «звука нет», принятым по опечатке."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "", "mode": "t2va"})["id"]
    status, payload = srv.post_json_raw(
        f"/api/chat/{sid}/message", {"text": "х", "prompt": "", "set_mode": "видео"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), payload
    assert sorted(payload["error"]["detail"]["modes"]) == sorted(web.CHAT_MODES), payload
    saved = srv.get_json(f"/api/chat/{sid}")
    assert saved["mode"] == "t2va", "отказанный set_mode не должен был перезаписать режим сессии"
    assert saved["messages"] == [], "отказанный ход не должен был лечь в историю"


# -- загрузка кадра (A7) ------------------------------------------------------------------------

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_an_uploaded_frame_lands_in_the_outdirs_uploads_directory_and_is_returned_as_a_path(
        _serve):
    srv = _serve()
    status, answer = srv.upload_raw(_PNG_BYTES, "start.png")
    assert (status, answer["ok"]) == (200, True), answer
    saved = Path(answer["path"])
    assert saved.is_file()
    assert saved.read_bytes() == _PNG_BYTES
    assert saved.parent == (Path(srv.root) / "uploads").resolve()
    assert saved.name.endswith("-start.png")


def test_a_non_image_upload_is_refused_as_bad_image(_serve):
    """Суффикс — та же проверка, что и у кадра сессии (`CHAT_IMAGE_SUFFIXES`), только раньше:
    здесь она не пускает файл на диск вовсе, а не только в разговор с моделью."""
    srv = _serve()
    status, answer = srv.upload_raw(b"not a picture", "notes.txt")
    assert (status, answer["error"]["code"]) == (400, "bad_image"), answer
    assert not (Path(srv.root) / "uploads").exists(), "отказ не должен создавать файл на диске"


def test_an_upload_over_the_size_limit_is_refused_as_bad_image(_serve, monkeypatch):
    monkeypatch.setattr(web, "CHAT_IMAGE_MAX_BYTES", 8)
    srv = _serve()
    status, answer = srv.upload_raw(_PNG_BYTES, "big.png")
    assert (status, answer["error"]["code"]) == (400, "bad_image"), answer
    assert not (Path(srv.root) / "uploads").exists(), "отказ не должен создавать файл на диске"


def test_a_traversal_filename_does_not_escape_the_uploads_directory(_serve):
    """`X-Filename: ../x.png` — тот же побег из корня, что и путь кадра сессии
    (`test_a_keyframe_outside_every_root_is_refused_when_the_session_opens`), только тут имя
    приходит не путём, а заголовком, который и берётся за basename в `sanitize_upload_name`."""
    srv = _serve()
    status, answer = srv.upload_raw(_PNG_BYTES, "../x.png")
    assert (status, answer["ok"]) == (200, True), answer
    saved = Path(answer["path"])
    # Файл создан ВНУТРИ uploads/, а не поднялся на уровень выше -- побег не удался.
    assert saved.parent == (Path(srv.root) / "uploads").resolve()
    assert saved.is_file()
    assert not (Path(srv.root) / "x.png").exists(), "имя не должно было сбежать из uploads/"
    # Имя очищено: разделитель не пережил sanitize_upload_name, «..» тоже не осталось.
    assert ".." not in saved.name
    assert "/" not in saved.name


def test_a_cyrillic_or_spaced_filename_becomes_a_safe_name(_serve):
    srv = _serve()
    status, answer = srv.upload_raw(_PNG_BYTES, "Кадр номер 1.png")
    assert (status, answer["ok"]) == (200, True), answer
    saved = Path(answer["path"])
    assert saved.is_file()
    # Только безопасный алфавит остался в имени; ни кириллицы, ни пробела.
    assert all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in saved.name), saved.name
    assert saved.suffix == ".png"


def test_an_upload_without_x_filename_is_a_bad_request(_serve):
    srv = _serve()
    status, answer = srv.upload_raw(_PNG_BYTES, None)
    assert (status, answer["error"]["code"]) == (400, "bad_request"), answer


# -- провайдеры и плашка модели ------------------------------------------------------------------


def test_the_providers_route_lists_the_roster_without_the_token(_serve):
    srv = _serve(providers={**_external(1), "qwen-local": {**_LLAMA, "port": 1}},
                 active="qwen-local", env="OPENROUTER_API_KEY=sk-very-secret\n")
    listed = srv.get_json("/api/providers")
    assert listed["active"] == "qwen-local"
    by_name = {row["name"]: row for row in listed["providers"]}
    assert by_name["qwen-local"]["available"] is True
    assert by_name["openrouter"]["type"] == "openai"
    assert "sk-very-secret" not in json.dumps(listed, ensure_ascii=False)


def test_a_provider_without_its_token_is_refused_with_its_own_reason(_serve):
    srv = _serve(providers={"openrouter": {"type": "openai", "base_url": "http://127.0.0.1:1",
                                           "model": "m", "api_key_env": "OPENROUTER_API_KEY"}},
                 active="openrouter")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (409, "provider_unavailable")
    assert "OPENROUTER_API_KEY" in payload["error"]["message"]


def test_a_provider_that_does_not_answer_keeps_its_own_code_at_the_http_boundary(_serve,
                                                                                 fake_llama):
    """`ProviderError` — доменное исключение; на границе HTTP оно обязано стать 502 со своим
    кодом, а не `internal_error` от последнего перехватчика."""
    port = fake_llama.port
    fake_llama.close()  # порт больше никто не слушает
    srv = _serve(providers=_external(port), active="openrouter")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (502, "chat_unreachable")
    assert srv.get_json(f"/api/chat/{sid}")["messages"] == [], "неудачный ход не пишется в историю"


def test_a_model_answering_valid_json_that_is_not_a_turn_is_a_502_not_a_500(_serve):
    """`null` — корректный JSON, и `provider.chat` его разбирает молча. Маршрут обязан назвать
    это ошибкой модели (502 `bad_model_json`), а не своей (`internal_error`, 500)."""
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": "null"}}]})
    try:
        srv = _serve(providers=_external(fake.port), active="openrouter")
        sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
        status, payload = srv.post_json_raw(f"/api/chat/{sid}/message",
                                            {"text": "x", "prompt": ""})
    finally:
        fake.close()
    assert (status, payload["error"]["code"]) == (502, "bad_model_json")


def test_the_llm_plate_follows_the_health_endpoint_and_unload_asks_for_a_shutdown(_serve,
                                                                                  fake_llama,
                                                                                  monkeypatch):
    """`shutdown()` подменён: настоящий делает `pkill -f llama-server` и убил бы модель, поднятую
    на этой машине руками, ради проверки одной строки маршрута."""
    srv = _serve(providers_port=fake_llama.port)
    assert srv.get_json("/api/llm") == {"ok": True, "status": "up", "provider": "qwen-local"}
    killed: list[str] = []
    monkeypatch.setattr(provider.LlamaLocal, "shutdown", lambda self: killed.append(self.name))
    assert srv.post_json("/api/llm/unload", {})["status"] == "down"
    assert killed == ["qwen-local"]


def test_the_llm_plate_is_up_when_a_non_active_local_provider_is_resident(_serve, fake_llama):
    """Finding I1: `providers.json` can list more than one `llama-local` provider (the real
    roster does -- `qwen-local` and `gemma-local`, both under one `active` field), and the chat
    page's per-turn provider dropdown can raise one that is not `active` without ever touching that
    field. The plate used to ask only the active provider's port, so "active external,
    `gemma-local` resident elsewhere" read as `down` -- exactly the state that let the worker start
    a generation next to a resident 30 GB model.
    """
    srv = _serve(providers={**_external(1), "gemma-local": {**_LLAMA, "port": fake_llama.port}},
                 active="openrouter")
    assert srv.get_json("/api/llm") == {"ok": True, "status": "up", "provider": "gemma-local"}


def test_llm_unload_kills_a_resident_local_provider_even_when_it_is_not_active(_serve, fake_llama,
                                                                                monkeypatch):
    """Same gap as above, the unload side: `POST /api/llm/unload` used to look up only the active
    provider (`_active_provider`), so with `openrouter` active it found no `LlamaLocal` at all and
    never touched the resident `gemma-local` -- the human clicks "free the GPU" and nothing frees.
    """
    srv = _serve(providers={**_external(1), "gemma-local": {**_LLAMA, "port": fake_llama.port}},
                 active="openrouter")
    killed: list[str] = []
    monkeypatch.setattr(provider.LlamaLocal, "shutdown", lambda self: killed.append(self.name))
    assert srv.post_json("/api/llm/unload", {})["status"] == "down"
    assert killed == ["gemma-local"]


def test_without_a_roster_the_plate_is_down_and_a_turn_says_which_provider_is_missing(_serve):
    """Ни providers.json, ни активного провайдера: сервер обязан отвечать, а не падать."""
    srv = _serve(roster=False)
    assert srv.get_json("/api/llm")["status"] == "down"
    assert srv.get_json("/api/providers") == {"ok": True, "active": None, "providers": []}
    assert srv.post_json("/api/llm/unload", {})["status"] == "down"
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (409, "provider_unavailable")


def test_every_chat_route_names_the_body_fields_it_takes(_serve):
    """Аллоулист полей тела — правило этого сервера (`_json_request`): поле, которое маршрут не
    знает, отказ, а не молчаливое игнорирование."""
    srv = _serve()
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                                      "promt": "опечатка"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message",
                                        {"text": "x", "prompt": "", "sytem": "нет"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


# -- сквозной путь модалки -----------------------------------------------------------------------


def test_a_chat_opened_from_a_library_prompt_survives_a_turn_end_to_end(_serve, fake_llama):
    """Задача 8, весь путь модалки сразу: открыть сессию от промпта библиотеки, сделать ход и
    получить назад то, из чего страница рисует обе половины окна.

    Проверяется не только ответ. После перезагрузки модалка восстанавливает окно промпта из
    `prompt_struct` сессии — и `prompt` при этом обязан остаться тем текстом, с которым сессию
    открыли: ход его не переписывает (так устроен маршрут), и страница, читающая `prompt`
    вместо `prompt_struct`, показала бы промпт до правки. Обе половины этого правила стоят
    рядом здесь, потому что порознь каждая выглядит верной.
    """
    srv = _serve(providers_port=fake_llama.port)
    # `duration: 6` — Fix round 1 (review, Important): `chat-prompt-open` («Обсудить») did not
    # hand the form's `#duration` to `openChatModal` at all, unlike `mode` right beside it in the
    # same object literal, so a chat opened from a library prompt always got the server's own
    # ten-second default regardless of what the form said. This is the server half of that
    # contract -- whatever number a fixed `chat-prompt-open` sends is the number the session
    # remembers, the same way `test_a_session_opened_from_a_job_carries_the_jobs_declared_duration`
    # pins the `job`-source half.
    opened = srv.post_json("/api/chat", {"source": {"kind": "prompt", "name": "x.txt"},
                                         "prompt": "старый текст", "mode": "t2va",
                                         "duration": 6})
    sid = opened["id"]

    answer = srv.post_json(f"/api/chat/{sid}/message",
                           {"text": "сделай мрачнее", "prompt": "старый текст"})
    assert answer["reply"] == "Сделал мрачнее."
    assert answer["prompt"]["integrated_multimodal_description"] == "[Shot 1] Live-action…"
    assert answer["warning"] is None, "кадра у t2va-сессии нет, и оговорке взяться неоткуда"

    saved = json.loads((Path(srv.root) / "chat" / f"{sid}.json").read_text(encoding="utf-8"))
    assert saved["source"] == {"kind": "prompt", "name": "x.txt"}, (
        "источник решает, что делает кнопка завершения, и обязан пережить ход")
    assert saved["duration"] == 6
    assert [(m["role"], m["content"]) for m in saved["messages"]] == [
        ("user", "сделай мрачнее"), ("assistant", "Сделал мрачнее.")]
    assert saved["prompt_struct"] == answer["prompt"]
    assert saved["prompt"] == "старый текст"


def test_reading_a_session_that_is_not_there_does_not_create_the_chat_directory(_serve):
    """`_chat_dir` вызывался с `mkdir` из обоих путей, включая чтение.

    GET по несуществующей сессии — это то, что делает страница, открытая по старой ссылке
    `/#chat/<id>` после того, как сессию удалили: чтение оставляло за собой пустой `chat/` в
    выводном каталоге. Каталог, который создаёт чтение, — маленькая ложь о состоянии («чат тут
    был») и, для outdir на внешнем диске, запись туда, где её никто не просил.
    """
    srv = _serve()
    chat_dir = Path(srv.root) / "chat"
    assert not chat_dir.exists(), "фикстура обязана начинать с чистого outdir"

    status, payload = srv.get_json_raw("/api/chat/deadbeef")
    assert (status, payload["error"]["code"]) == (404, "chat_not_found"), payload
    assert not chat_dir.exists(), "чтение не создаёт каталог"

    # …а создание — создаёт: тот же каталог, тот же вызов, разница только в намерении.
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    assert (chat_dir / f"{sid}.json").is_file()


def _corrupt_session(root: Path, sid: str, body) -> None:
    """Сессия, записанная мимо сервера — ровно то, что делает рука в редакторе."""
    directory = Path(root) / "chat"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{sid}.json").write_text(json.dumps(body), encoding="utf-8")


@pytest.mark.parametrize("body", [
    {"id": "hand", "prompt": "текст"},                        # `messages` не написали вовсе
    {"id": "hand", "messages": "две реплики"},                # строка вместо списка
    {"id": "hand", "messages": [{"role": "user"}]},           # реплика без content
    {"id": "hand", "messages": ["сделай мрачнее"]},           # реплика не объект
    ["сессия", "списком"],                                    # корень вообще не объект
])
def test_a_hand_edited_session_is_a_named_refusal_and_not_a_five_hundred(_serve, fake_llama, body):
    """Файл сессии правят руками — это JSON в каталоге, который человек видит.

    `session["messages"]` на файле без этого ключа кидал KeyError, и он же долетал до сети как
    `internal_error` 500: страница говорила «сервер споткнулся» про файл, который сама же и не
    писала, а в терминале оставался трейсбек, выглядящий как баг сервера. Отказ по коду —
    единственное, из чего понятно, что чинить надо файл.

    Проверяется и ход, и чтение: страница открывает сессию раньше, чем делает в ней ход, и
    отдать ей битый файл как исправный — значит перенести тот же KeyError в браузер.
    """
    srv = _serve(providers_port=fake_llama.port)
    _corrupt_session(srv.root, "hand", body)

    status, payload = srv.post_json_raw("/api/chat/hand/message", {"text": "x", "prompt": ""})
    assert status != 500, payload
    assert payload["error"]["code"] == "chat_corrupt", payload
    assert status == web.ERROR_STATUS["chat_corrupt"], payload

    status, payload = srv.get_json_raw("/api/chat/hand")
    assert status != 500, payload
    assert payload["error"]["code"] == "chat_corrupt", payload

    assert fake_llama.requests == [], "битый файл — не повод платить за ход"


@pytest.mark.parametrize("reply", [42, None, {"текст": "да"}, ["а", "б"], True])
def test_a_reply_that_is_not_a_string_is_refused_instead_of_bricking_the_session(_serve, reply):
    """Ход с нестроковым `reply` окирпичивал сессию — навсегда, и руками сервера.

    `reply = turn.get("reply") or ""` не спрашивал тип. Провайдер, игнорирующий
    `response_format` (внешние это делают: схема — просьба, а не гарантия), отвечает
    `{"reply": 42}`, число уходит в `messages` как `content`, ход возвращает 200 — а следующее
    же чтение упирается в `_check_session_shape`, который правильно говорит «реплика без
    строкового content» и отказывает `chat_corrupt`. С этого места сессия мертва: 409 и на GET, и
    на каждый следующий ход, и починить её можно только редактором. Файл написал сервер, а
    выглядит это как порча руками.

    Отказ — тот же `bad_model_json` и тем же приёмом, что проверка `isinstance(turn, dict)`
    строкой выше: это ровно «модель не удержала формат», а не повод молча позвать `str()` —
    приведение спрятало бы `42` в ленту как реплику модели.

    `True` в списке не для красоты: `isinstance(True, int)` истинно, и любая проверка «число ли
    это» пропустила бы булево. Вопрос задаётся один — строка ли.
    """
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": json.dumps(
        {"reply": reply, "prompt": None})}}]})
    try:
        srv = _serve(providers_port=fake.port)
        sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "старый текст"})["id"]

        status, payload = srv.post_json_raw(f"/api/chat/{sid}/message",
                                            {"text": "мрачнее", "prompt": "старый текст"})
        assert status == 502, payload
        assert payload["error"]["code"] == "bad_model_json", payload
    finally:
        fake.close()

    # Сессия цела: ни половины обмена в ленте, ни `chat_corrupt` на чтении.
    got = srv.get_json(f"/api/chat/{sid}")
    assert got["messages"] == [], "отказавший ход не оставляет за собой ничего"
    assert got["prompt"] == "старый текст"


def test_a_session_the_server_itself_wrote_is_never_called_corrupt(_serve, fake_llama):
    """Обратная сторона: проверка формы обязана пропускать всё, что пишет сам сервер — и пустую
    сессию сразу после создания, и её же после хода."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    assert srv.get_json(f"/api/chat/{sid}")["messages"] == []
    srv.post_json(f"/api/chat/{sid}/message", {"text": "мрачнее", "prompt": ""})
    assert len(srv.get_json(f"/api/chat/{sid}")["messages"]) == 2


# -- дублирование задачи -------------------------------------------------------------------------


def _submit_job(queue_root, outdir, tag: str, note: str = "") -> q.Job:
    """A pending job whose `output_stem` follows the real `outdir/h3-<tag>-<W>x<H>` shape, so
    `/duplicate`'s tag rewrite (`_duplicate_tag_candidates` in `web.py`) is exercised the same way
    it would be against a job a real submission produced.
    """
    stem = outdir / f"h3-{tag}-896x512"
    return q.submit(queue_root, ["generate", "--tag", tag], note,
                    {"output_stem": str(stem)}, {"seconds": 1})


def test_duplicating_a_pending_job_adds_a_second_pending_job_with_the_same_note(_serve):
    """The source job is untouched, and the copy keeps the note but not the output name -- that
    name is always taken, by the source job itself, still sitting in `pending/`.
    """
    srv = _serve()
    job = _submit_job(srv.queue_root, srv.root, "ждёт", note="заметка")

    status, answer = srv.post_json_raw(f"/api/jobs/{job.id}/duplicate", None)
    assert status == 200, answer
    new_id = answer["id"]
    assert new_id != job.id

    jobs, broken = q.scan(srv.queue_root)
    assert broken == []
    by_id = {row.id: row for row in jobs}
    assert by_id[job.id].state == "pending"
    assert by_id[new_id].state == "pending"
    assert by_id[new_id].note == "заметка"
    assert by_id[new_id].output_stem != by_id[job.id].output_stem


def test_duplicating_a_finished_job_lands_a_new_pending_job(_serve):
    """A `done` source is found (not just `pending`), left exactly as it was, and its own artifact
    on disk -- what "finished" means in practice -- does not stop the copy from queueing.
    """
    srv = _serve()
    job = _submit_job(srv.queue_root, srv.root, "готово", note="из готовой")
    running = q.claim(srv.queue_root)
    q.finish(srv.queue_root, running.id, 0, "ok")
    # `job.output_stem` now sits inside the job's own subdirectory (task A6); a real run would
    # have created it (`RunSpec.outdir.mkdir(...)` -- see `test_run_generate_creates_a_nonexistent_
    # outdir` in `test_cli.py`), so this stands in for that.
    Path(job.output_stem).parent.mkdir(parents=True, exist_ok=True)
    Path(f"{job.output_stem}.mp4").write_bytes(b"video")

    status, answer = srv.post_json_raw(f"/api/jobs/{job.id}/duplicate", None)
    assert status == 200, answer
    new_id = answer["id"]

    jobs, broken = q.scan(srv.queue_root)
    assert broken == []
    by_id = {row.id: row for row in jobs}
    assert by_id[job.id].state == "done", "duplicating a finished job does not touch it"
    assert by_id[new_id].state == "pending"
    assert by_id[new_id].note == "из готовой"
    assert by_id[new_id].output_stem != job.output_stem


def test_duplicating_a_failed_job_lands_a_new_pending_job(_serve):
    """Копия провалившейся — главный сценарий кнопки «Копия», и он был непроверен.

    Задача падает по причине, которая к самой задаче отношения не имеет (кончилось место, Мак
    заснул, работника убили), и повторить её — первое, что делает человек утром; на строке
    `fail` кнопка «Копия» единственная, что там вообще есть (`finishedRowHtml`). Тест дубликата
    `done` этого не покрывает: `done` и `failed` — разные каталоги очереди, и маршрут ищет
    исходную задачу перебором состояний.

    Имя вывода у копии всё равно новое, хотя исходное свободно (файла нет — прогон не дошёл до
    записи): `_duplicate_tag_candidates` переписывает тег, не спрашивая, чем кончился источник, и
    это правильно — иначе повтор затирал бы логи и превью того прогона, который и разбирают.
    """
    srv = _serve()
    job = _submit_job(srv.queue_root, srv.root, "упало", note="из упавшей")
    running = q.claim(srv.queue_root)
    q.finish(srv.queue_root, running.id, 1, "MemoryError: metal")

    status, answer = srv.post_json_raw(f"/api/jobs/{job.id}/duplicate", None)
    assert status == 200, answer
    new_id = answer["id"]

    jobs, broken = q.scan(srv.queue_root)
    assert broken == []
    by_id = {row.id: row for row in jobs}
    assert by_id[job.id].state == "failed", "копия не трогает упавшую задачу"
    assert by_id[job.id].exit_code == 1
    assert by_id[new_id].state == "pending"
    assert by_id[new_id].note == "из упавшей"
    assert by_id[new_id].output_stem != job.output_stem


def test_duplicating_a_job_gets_its_own_subdirectory_not_the_sources(_serve):
    """Task A6, through the actual `/duplicate` route (`_duplicate_job` in `web.py`), which
    resubmits the source job's own `args` -- already carrying `--outdir <source's own
    subdirectory>` -- with only `--tag` rewritten. Without `queue._base_outdir` stripping that
    existing subdirectory first, the copy would nest one level *inside* the source's own directory
    rather than land beside it: `output_stem != output_stem` alone (already asserted by the other
    duplicate tests) does not catch that, since a nested path is unequal too.
    """
    srv = _serve()
    job = _submit_job(srv.queue_root, srv.root, "kot-italy", note="")
    source_dir = Path(job.output_stem).parent
    assert source_dir.parent == srv.root, (
        "the fixture must submit a job whose own subdirectory sits directly under the outdir, or "
        "this test cannot tell 'beside' from 'nested inside' apart")

    status, answer = srv.post_json_raw(f"/api/jobs/{job.id}/duplicate", None)
    assert status == 200, answer
    new_id = answer["id"]

    jobs, broken = q.scan(srv.queue_root)
    assert broken == []
    by_id = {row.id: row for row in jobs}
    duplicate_dir = Path(by_id[new_id].output_stem).parent

    assert duplicate_dir != source_dir, "the duplicate must not reuse the source's own directory"
    assert duplicate_dir.parent == srv.root, (
        f"the duplicate's directory must be a sibling of the source's, directly under the outdir "
        f"-- got {duplicate_dir}, nested under {duplicate_dir.parent}")


def test_duplicating_an_unknown_job_is_a_named_404(_serve):
    srv = _serve()
    status, answer = srv.post_json_raw("/api/jobs/does-not-exist/duplicate", None)
    assert status == 404, answer
    assert answer["error"]["code"] == "not_found", answer


def test_duplicating_a_job_with_a_prompt_file_gets_its_own_snapshot_not_the_sources(_serve):
    """Fix round 1. `queue.submit` only re-snapshots the prompt when `prompt_text` is given;
    without it, `args` pass through untouched, and untouched here means `--prompt-file` still
    names the SOURCE job's own snapshot (`queue/prompts/<source-id>.txt`). `queue.cancel` deletes
    that file the moment the source job is withdrawn from `pending/`, so a duplicate that kept
    pointing at it would sit fine until the worker actually claimed it, then fail on an unreadable
    `--prompt-file` -- long after whoever clicked "Копия" had walked away.
    """
    srv = _serve()
    stem = srv.root / "h3-сцена-896x512"
    job = q.submit(srv.queue_root,
                   ["generate", "--tag", "сцена", "--prompt-file", "PLACEHOLDER"],
                   "", {"output_stem": str(stem)}, {"seconds": 1},
                   prompt_source="prompts/scene.txt", prompt_text="Кот на подоконнике.")
    source_snapshot = Path(job.args[job.args.index("--prompt-file") + 1])
    assert source_snapshot == srv.queue_root / "prompts" / f"{job.id}.txt"

    status, answer = srv.post_json_raw(f"/api/jobs/{job.id}/duplicate", None)
    assert status == 200, answer
    new_id = answer["id"]

    q.cancel(srv.queue_root, job.id)  # a real "cancel the source" -- deletes source_snapshot
    assert not source_snapshot.exists()

    jobs, broken = q.scan(srv.queue_root)
    assert broken == []
    new_job = next(j for j in jobs if j.id == new_id)
    new_snapshot = Path(new_job.args[new_job.args.index("--prompt-file") + 1])
    assert new_snapshot != source_snapshot, (
        "the duplicate must not point at the source job's own snapshot")
    assert new_snapshot == srv.queue_root / "prompts" / f"{new_id}.txt"
    assert new_snapshot.exists(), "cancelling the source must not break the duplicate"
    assert new_snapshot.read_text(encoding="utf-8") == "Кот на подоконнике."
