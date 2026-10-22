"""The studio API (spec §4.2 `studio-api`, §7): FastAPI over `ops`, the SSE journal (O21) and the
built master under `/studio`."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from h3_48gb.studio import assets, config, db, events, ops, schemas
from h3_48gb.studio.errors import StudioError
from h3_48gb.studio.llm import Llm, ProviderLlm

WEB_DIST = Path(__file__).parent / "web" / "dist"
#: Request body limits — the guard against junk instead of a cap on the number of scenes (a project
#: has no upper bound, §4.4). 2 MiB holds thousands of scene cards; uploads get the asset limit.
MAX_BODY_BYTES = 2 * 1024 * 1024
UPLOAD_PATH = "/api/studio/assets"
MAX_UPLOAD_BODY_BYTES = 26 * 1024 * 1024
#: The studio listens on the LAN without a login (one user, home network — plan ruling R26).
#: Host names it answers to; anything else is a DNS-rebinding attempt and gets 400. The server's
#: LAN IP comes from STUDIO_ALLOWED_HOSTS (compose sets it).
DEFAULT_ALLOWED_HOSTS = ("alex-neuro", "localhost", "127.0.0.1")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BACKUP_EVERY_S = 3600
#: SSE: journal poll period and the idle heartbeat (a `: ping` comment keeps proxies from closing).
SSE_POLL_S = 0.25
SSE_HEARTBEAT_S = 15.0


def allowed_hosts_from_env(environ=None) -> list[str]:
    raw = (os.environ if environ is None else environ).get("STUDIO_ALLOWED_HOSTS", "")
    extra = [h.strip() for h in raw.split(",") if h.strip()]
    return list(dict.fromkeys([*DEFAULT_ALLOWED_HOSTS, *extra]))


_DEFAULT_PORTS = {"http": 80, "https": 443}


def parse_origin(value: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of a serialized origin, or None when it is not one: `null`, a
    relative `//host`, a path, a query, credentials, a bad port, an unclosed `[` — anything the
    browser would never send as an Origin of a real page."""
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        return None
    return parts.scheme, parts.hostname.lower(), port if port is not None else _DEFAULT_PORTS[parts.scheme]


def request_origin(scheme: str, host_header: str) -> tuple[str, str, int] | None:
    return parse_origin(f"{scheme}://{host_header}")


class SameOriginGuard:
    """ASGI middleware: a changing request (POST/PUT/PATCH/DELETE) from another site gets 403.
    Allowed: no Origin and no Sec-Fetch-Site (curl, scripts); Sec-Fetch-Site only `same-origin`
    or `none`; an Origin whose (scheme, host, port) equals the request's own. Everything else —
    another scheme or port, `same-site`, `null`, a malformed Origin — is refused with 403, never
    a 500."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope["headers"]}
            fetch_site = headers.get("sec-fetch-site")
            origin = headers.get("origin")
            refused = fetch_site is not None and fetch_site not in ("same-origin", "none")
            if origin is not None and not refused:
                mine = request_origin(scope.get("scheme", "http"), headers.get("host", ""))
                theirs = parse_origin(origin)
                refused = theirs is None or mine is None or theirs != mine
            if refused:
                body = StudioError(403, "cross_origin", "Запрос с чужого сайта отклонён",
                                   {"origin": origin, "sec_fetch_site": fetch_site}).body()
                payload = json.dumps(body, ensure_ascii=False).encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(payload)).encode())]})
                await send({"type": "http.response.body", "body": payload})
                return
        await self.app(scope, receive, send)


class BodyLimit:
    """ASGI middleware: 413 `body_too_large` when the declared or the actually received body is
    over the limit for the path. The body is read (up to the limit) before the app sees it, so a
    chunked body with no length is counted too, and FastAPI's own body parsing cannot swallow
    the refusal into a 400."""

    def __init__(self, app, *, default: int, upload: int, upload_path: str):
        self.app, self.default, self.upload, self.upload_path = app, default, upload, upload_path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.upload if scope["path"] == self.upload_path else self.default
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            return await self._refuse(send, limit)
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return                                  # the client went away
            total += len(message.get("body", b""))
            if total > limit:
                return await self._refuse(send, limit)
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        replay = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        replayed = False

        async def receive_again():
            nonlocal replayed
            if not replayed:
                replayed = True
                return replay
            return await receive()                      # later calls: disconnect (SSE)

        await self.app(scope, receive_again, send)

    @staticmethod
    async def _refuse(send, limit: int):
        body = StudioError(413, "body_too_large", f"Запрос больше {limit // (1024 * 1024)} МБ",
                           {"max_bytes": limit}).body()
        payload = json.dumps(body, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})


def create_app(outdir: Path, *, web_dist: Path = WEB_DIST,
               allowed_hosts: list[str] | None = None, llm: Llm | None = None) -> FastAPI:
    home = config.studio_dir(outdir)
    home.mkdir(parents=True, exist_ok=True)
    llm = llm or ProviderLlm(home, home / "gpu-mac.lock")
    db_path = home / "studio.sqlite"
    boot = db.connect(db_path)
    try:
        db.migrate(boot)
    finally:
        boot.close()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def backups():
            while True:
                await asyncio.sleep(BACKUP_EVERY_S)
                await asyncio.to_thread(db.backup_once, db_path, home / "backups",
                                        now=datetime.now())
        task = asyncio.create_task(backups())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="h3 studio", version="0.1.0", lifespan=lifespan)
    # Added last = runs first: Host check, then the same-origin guard, then the body limit.
    app.add_middleware(BodyLimit, default=MAX_BODY_BYTES, upload=MAX_UPLOAD_BODY_BYTES,
                       upload_path=UPLOAD_PATH)
    app.add_middleware(SameOriginGuard)
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=allowed_hosts if allowed_hosts is not None
                       else allowed_hosts_from_env())
    app.state.outdir = outdir
    app.state.db_path = db_path

    @app.exception_handler(StudioError)
    async def _studio_error(_request: Request, exc: StudioError):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError):
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": {"code": "args_invalid", "message": "Неверные данные запроса",
                                       "detail": {"errors": errors}}}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException):
        code = "not_found" if exc.status_code == 404 else "http_error"
        return JSONResponse({"error": {"code": code, "message": str(exc.detail), "detail": {}}},
                            status_code=exc.status_code)

    def get_conn():
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def project_exists(project_id: str, conn=Depends(get_conn)) -> str:
        # A dependency, so a missing project answers 404 before the body is validated (§7).
        ops.require_project(conn, project_id)
        return project_id

    api = "/api/studio"

    @app.get(f"{api}/health")
    def health(conn=Depends(get_conn)):
        return {"ok": True, "schema_version": conn.execute("PRAGMA user_version").fetchone()[0]}

    @app.get(f"{api}/projects")
    def list_projects(conn=Depends(get_conn)):
        return ops.list_projects(conn)

    @app.post(f"{api}/projects", status_code=201)
    def create_project(body: schemas.ProjectCreate, conn=Depends(get_conn)):
        return ops.create_project(conn, title=body.title, frame_format=body.frame_format,
                                  idea_text=body.idea_text)

    @app.get(f"{api}/projects/{{project_id}}")
    def get_project(project_id: str = Depends(project_exists), conn=Depends(get_conn)):
        return ops.snapshot(conn, project_id)

    @app.patch(f"{api}/projects/{{project_id}}")
    def patch_project(body: schemas.ProjectPatch, project_id: str = Depends(project_exists),
                      conn=Depends(get_conn)):
        return ops.update_project(conn, project_id, expected_revision=body.expected_revision,
                                  title=body.title, frame_format=body.frame_format)

    @app.post(f"{api}/assets", status_code=201)
    async def import_asset(file: UploadFile = File(...), kind: str | None = Form(default=None),
                           conn=Depends(get_conn)):
        data = await file.read(assets.MAX_ASSET_BYTES + 1)
        return await asyncio.to_thread(assets.import_asset, conn, home, data=data, kind=kind)

    @app.get(f"{api}/assets/{{asset_id}}/file")
    def asset_file(asset_id: str, conn=Depends(get_conn)):
        path, mime = assets.asset_file(conn, asset_id)
        return FileResponse(path, media_type=mime)

    @app.post(f"{api}/projects/{{project_id}}/cast", status_code=201)
    def add_cast(body: schemas.CastCreate, project_id: str = Depends(project_exists),
                 conn=Depends(get_conn)):
        return ops.add_cast(conn, project_id, expected_revision=body.expected_revision,
                            tag=body.tag, description_ru=body.description_ru,
                            assets=[a.model_dump() for a in body.assets],
                            description_en=body.description_en,
                            portrait_phrase=body.portrait_phrase, llm=llm)

    @app.get(f"{api}/events")
    async def stream_events(request: Request, after: int = 0,
                            last_event_id: int | None = Header(default=None)):
        start = max(after, last_event_id or 0)

        def read_batch(after_seq: int) -> list[dict]:
            # A connection per read, opened and closed inside the worker thread. A connection
            # shared across reads was closed by the generator's cleanup while a cancelled read
            # still ran in its thread — sqlite3 segfaulted (seen in the Docker image, 1 run in 8).
            conn = db.connect(db_path)
            try:
                return events.read_after(conn, after_seq)
            finally:
                conn.close()

        async def frames():
            seen = start
            idle = 0.0
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(read_batch, seen)
                for event in batch:
                    seen = event["seq"]
                    yield events.sse_frame(event)
                if batch:
                    idle = 0.0
                    continue
                await asyncio.sleep(SSE_POLL_S)
                idle += SSE_POLL_S
                if idle >= SSE_HEARTBEAT_S:
                    idle = 0.0
                    yield ": ping\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/")
    def root():
        return RedirectResponse("/studio/")

    @app.get("/studio")
    @app.get("/studio/{path:path}")
    def master(path: str = ""):
        index = web_dist / "index.html"
        if not index.is_file():
            raise StudioError(503, "web_not_built",
                              "Мастер не собран: npm --prefix h3_48gb/studio/web run build")
        candidate = (web_dist / path).resolve()
        if path and candidate.is_file() and web_dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)

    return app
