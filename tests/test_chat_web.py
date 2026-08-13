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
import http.client
import json
import threading
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
    opened = srv.post_json("/api/chat", {"source": {"kind": "prompt", "name": "x.txt"},
                                         "prompt": "старый текст", "mode": "t2va"})
    sid = opened["id"]

    answer = srv.post_json(f"/api/chat/{sid}/message",
                           {"text": "сделай мрачнее", "prompt": "старый текст"})
    assert answer["reply"] == "Сделал мрачнее."
    assert answer["prompt"]["integrated_multimodal_description"] == "[Shot 1] Live-action…"
    assert answer["warning"] is None, "кадра у t2va-сессии нет, и оговорке взяться неоткуда"

    saved = json.loads((Path(srv.root) / "chat" / f"{sid}.json").read_text(encoding="utf-8"))
    assert saved["source"] == {"kind": "prompt", "name": "x.txt"}, (
        "источник решает, что делает кнопка завершения, и обязан пережить ход")
    assert [(m["role"], m["content"]) for m in saved["messages"]] == [
        ("user", "сделай мрачнее"), ("assistant", "Сделал мрачнее.")]
    assert saved["prompt_struct"] == answer["prompt"]
    assert saved["prompt"] == "старый текст"


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
