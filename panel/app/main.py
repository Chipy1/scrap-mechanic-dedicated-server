from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

PANEL_USER = os.getenv("SM_PANEL_USER", "admin")
PANEL_PASSWORD = os.getenv("SM_PANEL_PASSWORD", "")
PANEL_SECRET = os.getenv("SM_PANEL_SECRET", "")
SESSION_TTL = int(os.getenv("SM_PANEL_SESSION_TTL", "43200"))
CONTROL_URL = os.getenv("SM_CONTROL_URL", "http://127.0.0.1:8080").rstrip("/")
API_TOKEN = os.getenv("SM_API_TOKEN", "")
VNC_HOST = os.getenv("SM_VNC_HOST", "127.0.0.1")
VNC_PORT = int(os.getenv("SM_VNC_PORT", "5900"))
NOVNC_DIR = Path(os.getenv("SM_NOVNC_DIR", "/opt/sm-panel/static/novnc"))
COOKIE = "sm_panel_session"

if not PANEL_PASSWORD or not PANEL_SECRET:
    raise RuntimeError("SM_PANEL_PASSWORD and SM_PANEL_SECRET are required")

app = FastAPI(title="Scrap Mechanic panel", docs_url=None, redoc_url=None)


def _sign(payload: str) -> str:
    return hmac.new(PANEL_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _new_session() -> str:
    payload = f"{PANEL_USER}:{int(time.time()) + SESSION_TTL}"
    return f"{payload}:{_sign(payload)}"


def _valid_session(value: str | None) -> bool:
    if not value:
        return False
    parts = value.split(":")
    if len(parts) != 3:
        return False
    user, expiry, signature = parts
    if user != PANEL_USER:
        return False
    try:
        if int(expiry) < time.time():
            return False
    except ValueError:
        return False
    expected = _sign(f"{user}:{expiry}")
    return hmac.compare_digest(signature, expected)


def _authed(request: Request) -> bool:
    return _valid_session(request.cookies.get(COOKIE))


def _login_page(error: str = "") -> str:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scrap Mechanic panel</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center;
  background:#11151b; color:#e6edf3; font:15px system-ui, sans-serif; }}
.card {{ width:min(420px, calc(100vw - 48px)); padding:28px; border-radius:14px;
  background:#1b222c; box-shadow:0 20px 60px #0008; }}
h1 {{ font-size:20px; margin:0 0 6px; }}
p {{ color:#9aa7b4; }}
label {{ display:block; margin:16px 0 6px; color:#b8c4d0; }}
input {{ width:100%; box-sizing:border-box; padding:11px 12px; border-radius:8px;
  border:1px solid #364252; background:#111820; color:#fff; font:inherit; }}
button {{ width:100%; margin-top:18px; padding:11px; border:0; border-radius:8px;
  background:#2f81f7; color:#fff; font-weight:600; cursor:pointer; }}
button:hover {{ background:#4b96ff; }}
.error {{ color:#ff8c8c; }}
</style>
</head>
<body><main class="card">
<h1>Scrap Mechanic</h1>
<p>Dedicated server panel</p>
{message}
<form method="post" action="/login">
<label for="username">Пользователь</label>
<input id="username" name="username" autocomplete="username" required>
<label for="password">Пароль</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Войти</button>
</form>
</main></body></html>"""


def _panel_page() -> str:
    return """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scrap Mechanic panel</title>
<style>
:root { color-scheme: dark; }
* { box-sizing:border-box; }
body { margin:0; background:#0f141a; color:#e6edf3; font:14px system-ui,sans-serif; }
header { display:flex; align-items:center; justify-content:space-between;
  padding:14px 20px; background:#171e27; border-bottom:1px solid #273241; }
header h1 { margin:0; font-size:17px; }
header a { color:#9fb0c2; text-decoration:none; }
main { display:grid; grid-template-columns:330px 1fr; gap:14px; padding:14px; height:calc(100vh - 52px); }
.side { display:flex; flex-direction:column; gap:12px; min-height:0; }
.card { background:#171e27; border:1px solid #273241; border-radius:10px; padding:14px; }
.card h2 { margin:0 0 10px; font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:#8fa0b2; }
.kv { display:grid; grid-template-columns:110px 1fr; gap:6px 10px; }
.kv span:nth-child(odd) { color:#8fa0b2; }
.kv span:nth-child(even) { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
button { border:0; border-radius:7px; padding:8px 10px; background:#2b6cb0; color:#fff;
  cursor:pointer; font:inherit; }
button:hover { background:#3981ca; }
.logs { flex:1; min-height:0; display:flex; flex-direction:column; }
pre { flex:1; min-height:0; overflow:auto; margin:0; padding:10px; border-radius:7px;
  background:#0b1016; color:#bfd3e3; font:12px/1.45 ui-monospace,monospace; white-space:pre-wrap; }
.vnc { min-height:0; background:#171e27; border:1px solid #273241; border-radius:10px; overflow:hidden; }
.vnc iframe { width:100%; height:100%; border:0; display:block; background:#000; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; background:#2b6cb0; }
.error { color:#ff8c8c; white-space:pre-wrap; }
@media (max-width:850px) { main { grid-template-columns:1fr; height:auto; } .vnc { height:75vh; } }
</style>
</head>
<body>
<header><h1>Scrap Mechanic · панель</h1><a href="/logout">Выйти</a></header>
<main>
  <aside class="side">
    <section class="card">
      <h2>Сервер</h2>
      <div class="kv" id="status">Загрузка…</div>
    </section>
    <section class="card">
      <h2>Действия</h2>
      <button id="backup">Создать бэкап</button>
      <p id="action" class="error"></p>
    </section>
    <section class="card logs">
      <h2>Лог</h2>
      <pre id="logs">Загрузка…</pre>
    </section>
  </aside>
  <section class="vnc">
    <iframe src="/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc/ws" allow="clipboard-read; clipboard-write"></iframe>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
function esc(v) { return String(v ?? "—"); }
function setKv(el, rows) {
  el.innerHTML = rows.map(([k,v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("");
}
async function api(path, opts) {
  const r = await fetch(path, Object.assign({credentials:"same-origin"}, opts || {}));
  if (r.status === 401 || r.status === 403) { location.href = "/login"; throw new Error("auth"); }
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}
async function refresh() {
  try {
    const d = await api("/api/panel/status");
    const s = d.server || {}, save = d.save || {};
    setKv($("status"), [
      ["Состояние", s.state],
      ["Игроки", s.players_online],
      ["Версия", s.version],
      ["Сейв", save.save_name],
      ["Совпадение", s.save_match],
      ["Бэкапы", (d.backups || {}).count],
    ]);
    const l = await api("/api/panel/logs?tail=120");
    $("logs").textContent = (l.lines || []).join("\\n") || "Лог пуст";
  } catch (e) { $("status").textContent = String(e); }
}
$("backup").onclick = async () => {
  $("action").textContent = "";
  try {
    const d = await api("/api/panel/backups", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({trigger:"manual"})});
    $("action").textContent = "Бэкап создан: " + d.id;
    refresh();
  } catch (e) { $("action").textContent = String(e); }
};
refresh();
setInterval(refresh, 5000);
</script>
</body></html>"""


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root(request: Request) -> HTMLResponse:
    if not _authed(request):
        return HTMLResponse(_login_page())
    return HTMLResponse(_panel_page())


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request) -> HTMLResponse:
    if _authed(request):
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_login_page())


@app.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    if username != PANEL_USER or not hmac.compare_digest(password, PANEL_PASSWORD):
        return HTMLResponse(_login_page("Неверный пользователь или пароль"), status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE,
        _new_session(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=False,
    )
    return response


@app.get("/logout")
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


async def _control(request: Request, method: str, path: str, **kwargs: Any) -> Response:
    if not _authed(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    headers = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {}
    extra_headers = kwargs.pop("headers", {})
    headers.update(extra_headers)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            result = await client.request(method, f"{CONTROL_URL}{path}", headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)
    content_type = result.headers.get("content-type", "application/json")
    return Response(result.content, status_code=result.status_code, media_type=content_type)


@app.get("/api/panel/status")
async def panel_status(request: Request) -> Response:
    return await _control(request, "GET", "/api/v1/status")


@app.get("/api/panel/players")
async def panel_players(request: Request) -> Response:
    return await _control(request, "GET", "/api/v1/players")


@app.get("/api/panel/logs")
async def panel_logs(request: Request, tail: int = 120) -> Response:
    return await _control(request, "GET", "/api/v1/logs", params={"tail": tail})


@app.post("/api/panel/backups")
async def panel_backup(request: Request) -> Response:
    body = await request.body()
    return await _control(
        request,
        "POST",
        "/api/v1/backups",
        content=body,
        headers={"Content-Type": "application/json"},
    )


@app.websocket("/vnc/ws")
async def vnc_ws(websocket: WebSocket) -> None:
    if not _valid_session(websocket.cookies.get(COOKIE)):
        await websocket.close(code=1008)
        return

    await websocket.accept(subprotocol=websocket.headers.get("sec-websocket-protocol"))
    try:
        reader, writer = await asyncio.open_connection(VNC_HOST, VNC_PORT)
    except OSError:
        await websocket.close(code=1011)
        return

    async def client_to_vnc() -> None:
        while True:
            data = await websocket.receive_bytes()
            writer.write(data)
            await writer.drain()

    async def vnc_to_client() -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            await websocket.send_bytes(data)

    tasks = [asyncio.create_task(client_to_vnc()), asyncio.create_task(vnc_to_client())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        await writer.wait_closed()
    try:
        await websocket.close()
    except Exception:
        pass


if NOVNC_DIR.is_dir():
    app.mount("/vnc", StaticFiles(directory=str(NOVNC_DIR), html=True), name="novnc")
else:
    @app.get("/vnc/{path:path}")
    async def missing_novnc(path: str) -> Response:
        return Response("noVNC assets are not installed", status_code=503)
