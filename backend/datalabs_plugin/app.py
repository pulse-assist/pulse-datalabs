"""Плагин DataLabs: ноутбуки Jupyter в «Пульсе».

Владелец: «DataLabs» в меню → JupyterLab (/lab), ноутбук в «Файлах» → Notebook (/open?ticket=…), на телефоне —
статичный просмотр (/view). Jupyter — /jupyter/… (HTTP и WebSocket) через прокси с токеном, корень — все «Файлы».
Агенты: pulse datalabs exec|sessions|save|reset|close (живое ядро), run|read|export|report (ноутбук целиком), check.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import site
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pulse_plugin import CommandError, Plugin, PulseApiError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import guard
from .jupyter import Jupyter
from .kernels import AgentKernels, KernelError
from .notebooks import Notebooks, RunError, flag, report_message
from .paths import PathError, Tree

log = logging.getLogger("datalabs")

plugin = Plugin()
app = plugin.app
APP_PATH = plugin.app_path.rstrip("/")
PUBLIC_URL = os.environ.get("PULSE_PUBLIC_URL")

tree = Tree.from_env(own_folder=plugin.folder, own_dir=plugin.files_dir)
workspace = tree.root if tree.root is not None else plugin.data_dir / "workspace"
lab = Jupyter(base_url=f"{APP_PATH}/jupyter/", root_dir=workspace, data_dir=plugin.data_dir,
              idle_minutes=int(plugin.setting("kernel_idle_minutes", 60) or 0))
kernels = AgentKernels(lab, tree, plugin.data_dir / "agents", plugin.files_dir)
notebooks = Notebooks(tree, lambda: int(plugin.setting("run_timeout_minutes", 30) or 30))

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "authorization", "cookie", "content-length"}
RESP_SKIP = {"connection", "keep-alive", "transfer-encoding", "content-security-policy"}
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def _lifespan(_app):
    global _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(60, read=300), follow_redirects=False)
    workspace.mkdir(parents=True, exist_ok=True)
    # %pip install в ноутбуке — в данные плагина (PYTHONUSERBASE): пакеты переживают обновление плагина
    Path(site.getusersitepackages()).mkdir(parents=True, exist_ok=True)
    await run_in_threadpool(lab.start)
    try:
        yield
    finally:
        await run_in_threadpool(lab.stop)
        await _client.aclose()


app.router.lifespan_context = _lifespan


# --- кто может дойти до Jupyter ----------------------------------------------------------------------------

def _allowed(headers) -> bool:
    return guard.allowed(headers.get("host"), headers.get("origin"), headers.get("x-forwarded-host"), PUBLIC_URL)


@app.middleware("http")
async def _guard(request: Request, call_next):
    if request.url.path not in ("/health", "/ready") and not _allowed(request.headers):
        return JSONResponse({"detail": "Запрос не от Пульса"}, status_code=403)
    return await call_next(request)


@app.get("/ready")
def ready() -> JSONResponse:
    """Health для Пульса: дерево «Файлов» читается (после перезапуска Пульса монтирование умирает — тогда 503,
    и Пульс перезапустит контейнер) и Jupyter жив (упал — поднимаем)."""
    if tree.root is not None and not tree.available():
        return JSONResponse({"ok": False, "error": "дерево «Файлов» недоступно — нужен перезапуск"}, status_code=503)
    lab.ensure()
    return JSONResponse({"ok": True, "jupyter": lab.alive(), "restarts": lab.restarts})


# --- страницы ----------------------------------------------------------------------------------------------

def _boot_page(target: str, text: str = "Открываю Jupyter…") -> HTMLResponse:
    """Тема Пульса → настройки Jupyter, затем переход. Фрейм same-origin: тему читаем у родителя."""
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>DataLabs</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
font:14px system-ui,sans-serif;color:#8a8f98;background:transparent}}</style></head>
<body><span>{html.escape(text)}</span><script>
(async () => {{
  let theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  try {{ const t = parent.document.documentElement.getAttribute("data-bs-theme"); if (t) theme = t === "dark" ? "dark" : "light"; }} catch (e) {{}}
  try {{ await fetch("theme", {{method: "POST", headers: {{"Content-Type": "application/json"}},
                               body: JSON.stringify({{theme}})}}); }} catch (e) {{}}
  location.replace({json.dumps(target)}.replace("__THEME__", theme));
}})();
</script></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/lab")
def lab_page() -> HTMLResponse:
    return _boot_page(f"{APP_PATH}/jupyter/lab")


@app.post("/theme")
async def theme(request: Request) -> dict:
    body = await request.json()
    try:
        await run_in_threadpool(lab.set_theme, str(body.get("theme", "")))
    except httpx.HTTPError:
        pass                       # Jupyter ещё поднимается — откроется в своей теме
    return {"ok": True}


def _ticket_path(ticket: str) -> str:
    try:
        return str(plugin.file_ticket(ticket).meta()["path"])
    except PulseApiError as exc:
        raise HTTPException(404, "Ссылка на файл устарела — откройте его в «Файлах» заново") from exc


@app.get("/open")
def open_file(ticket: str, mode: str = "edit") -> HTMLResponse:
    """Обработчик «Файлов»: ноутбук в Notebook 7 (дерево «Файлов» — корень Jupyter); телефон — просмотр."""
    path = _ticket_path(ticket)
    local = None
    if tree.root is not None:
        try:
            local = tree.resolve(path)
        except PathError:
            local = None
    if mode == "view" or local is None or not local.is_file():
        return _boot_page(f"view?ticket={quote(ticket)}&theme=__THEME__", "Открываю ноутбук…")
    return _boot_page(f"{APP_PATH}/jupyter/notebooks/{quote(tree.rel(local))}")


def _render(data: bytes, theme: str, notebook_url: str | None) -> str:
    import nbformat
    from nbconvert import HTMLExporter

    nb = nbformat.reads(data.decode("utf-8"), as_version=4)
    exporter = HTMLExporter(template_name="lab", theme="dark" if theme == "dark" else "light")
    body, _ = exporter.from_notebook_node(nb)
    if notebook_url:
        bar = (f'<div style="position:sticky;top:0;z-index:10;padding:6px 12px;text-align:right;'
               f'background:var(--jp-layout-color1,#fff);border-bottom:1px solid var(--jp-border-color2,#ddd)">'
               f'<a href="{html.escape(notebook_url)}" style="font:13px system-ui">Открыть в Jupyter</a></div>')
        marker = body.find(">", body.find("<body")) + 1
        body = body[:marker] + bar + body[marker:]
    return body


@app.get("/view")
def view(ticket: str, theme: str = "light") -> HTMLResponse:
    path = _ticket_path(ticket)
    try:
        data = plugin.file_ticket(ticket).read()
    except PulseApiError as exc:
        raise HTTPException(404, "Файл не читается") from exc
    link = None
    if tree.root is not None:
        try:
            if tree.resolve(path).is_file():
                link = f"{APP_PATH}/jupyter/notebooks/{quote(path)}"
        except PathError:
            link = None
    try:
        return HTMLResponse(_render(data, theme, link))
    except Exception as exc:  # noqa: BLE001 — битый ноутбук
        return HTMLResponse(f"<p style='font:14px system-ui;padding:16px'>Не получилось показать ноутбук: "
                            f"{html.escape(str(exc))}</p>", status_code=422)


# --- прокси к Jupyter ---------------------------------------------------------------------------------------

@app.api_route("/jupyter", methods=["GET"])
def jupyter_root() -> RedirectResponse:
    return RedirectResponse(f"{APP_PATH}/jupyter/lab")


@app.api_route("/jupyter/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def jupyter_http(path: str, request: Request) -> Response:
    await run_in_threadpool(lab.ensure)
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP and not k.lower().startswith(("x-forwarded-", "x-pulse-"))}
    headers.update(lab.headers)
    body = await request.body()
    upstream = _client.build_request(request.method, lab.url(path), params=list(request.query_params.multi_items()),
                                     headers=headers, content=body or None)
    try:
        response = await _client.send(upstream, stream=True)
    except httpx.ConnectError:
        return HTMLResponse("<meta http-equiv='refresh' content='2'><p style='font:14px system-ui;padding:16px'>"
                            "Jupyter запускается…</p>", status_code=503, headers={"Retry-After": "2"})
    out = StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                            background=BackgroundTask(response.aclose))
    out.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers.multi_items()
                       if k.lower() not in RESP_SKIP]
    return out


@app.websocket("/jupyter/{path:path}")
async def jupyter_ws(websocket: WebSocket, path: str) -> None:
    if not _allowed(websocket.headers):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    target = lab.ws_url(path) + (f"?{websocket.url.query}" if websocket.url.query else "")
    try:
        async with websockets.connect(target, additional_headers=lab.headers, max_size=None, ping_interval=20,
                                      open_timeout=30) as upstream:
            async def down() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            async def up() -> None:
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("text") is not None:
                            await upstream.send(message["text"])
                        elif message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                except WebSocketDisconnect:
                    return

            tasks = [asyncio.create_task(job()) for job in (down, up)]
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except Exception as exc:  # noqa: BLE001 — Jupyter перезапускается, ядро удалено
        log.info("ws %s: %s", path, exc)
    try:
        await websocket.close()
    except Exception:  # noqa: BLE001
        pass


# --- команды агентов ---------------------------------------------------------------------------------------

def command(name: str):
    """Команда агентов: ошибки путей, ядра и запуска — понятным текстом (400); синхронные — в пуле потоков."""
    def register(fn):
        async def run(args: dict):
            try:
                if asyncio.iscoroutinefunction(fn):
                    return await fn(args)
                return await run_in_threadpool(fn, args)
            except (PathError, KernelError, RunError, ValueError) as exc:
                raise CommandError(str(exc)) from exc
        plugin.command(name)(run)
        return fn
    return register


def _text(value) -> str | None:
    """Значение аргумента: «--title» без значения CLI присылает как true — считаем, что его нет."""
    return None if value in (None, "", True) else str(value)


def _int(value, default: int) -> int:
    try:
        return int(value) if _text(value) is not None else default
    except (TypeError, ValueError):
        raise ValueError(f"ожидалось число, а не «{value}»") from None


def _path(args: dict) -> str:
    path = _text(args.get("path"))
    if not path:
        raise ValueError("--path — ноутбук: путь в «Файлах» или $PULSE_FS_DIR/…")
    return path


def _code(args: dict) -> str:
    if _text(args.get("code_file")):
        target = tree.resolve(str(args["code_file"]))
        if not target.is_file():
            raise PathError(f"Нет такого файла: {args['code_file']}")
        return target.read_text(encoding="utf-8")
    return _text(args.get("code")) or ""


@command("exec")
async def exec_code(args: dict):
    code = await run_in_threadpool(_code, args)
    return await kernels.execute(_text(args.get("session")), code, _text(args.get("cwd")),
                                 _int(args.get("timeout"), 300))


@command("sessions")
async def list_sessions(args: dict):
    return await kernels.sessions()


@command("save")
def save_session(args: dict):
    path = _text(args.get("path"))
    if not path:
        raise ValueError("--path — куда сохранить ноутбук, например Аналитика/продажи.ipynb")
    return kernels.save(_text(args.get("session")), path, title=_text(args.get("title")),
                        errors=flag(args.get("errors")), overwrite=flag(args.get("overwrite")))


@command("reset")
async def reset_session(args: dict):
    return await kernels.reset(_text(args.get("session")))


@command("close")
async def close_session(args: dict):
    return await kernels.close(_text(args.get("session")))


@command("run")
def run_notebook(args: dict):
    return notebooks.run(_path(args), args.get("params"), _text(args.get("output")))


@command("read")
def read_notebook(args: dict):
    return notebooks.read(_path(args), _text(args.get("cells")), flag(args.get("outputs"), True),
                          _int(args.get("max_chars"), 20000))


@command("export")
def export_notebook(args: dict):
    return notebooks.export(_path(args), _text(args.get("to")) or "html", _text(args.get("output")),
                            flag(args.get("no_input")))


def _owner_timezone() -> str:
    try:
        return str(plugin.api.get("/api/settings/general").get("timezone") or "UTC")
    except Exception:  # noqa: BLE001 — Пульс недоступен: время отчёта по UTC
        return "UTC"


def _notify(title: str, text: str) -> None:
    for channels in (None, ["pulse"]):        # без бота Telegram — хотя бы в Пульс
        body = {"title": f"DataLabs · {title}", "text": text, **({"channels": channels} if channels else {})}
        try:
            plugin.api.post("/api/notify", body)
            return
        except PulseApiError as exc:
            log.warning("уведомление не ушло (%s): %s", channels or "по умолчанию", exc)


@command("report")
def report(args: dict):
    path = _path(args)
    result = notebooks.report(path, params=args.get("params"), to=_text(args.get("to")),
                              fmt=_text(args.get("format")) or "html", code=flag(args.get("code")),
                              tz=_owner_timezone())
    title = _text(args.get("title")) or Path(path).stem
    message = report_message(result, title)
    if flag(args.get("notify"), True):
        _notify(title, message)
    if not result["ok"]:
        # задание планировщика с этой командой помечается неудачным
        raise RunError(message + "\n" + json.dumps(result.get("error"), ensure_ascii=False)[:3000])
    return {**result, "message": message}


def _memory_limit() -> str:
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
    except OSError:
        return "не узнать"
    if raw == "max":
        return "без лимита"
    return f"{int(raw) / 1024 ** 3:.1f} ГБ"


@command("check")
async def check(args: dict):
    checks = []
    ok_tree = tree.available()
    checks.append({"title": "Файлы владельца", "ok": ok_tree,
                   "detail": f"корень Jupyter — {workspace}" if ok_tree else
                   "дерево «Файлов» не смонтировано — перезапустите плагин (pulse plugins restart datalabs)"})
    ready_ok = await run_in_threadpool(lab.ready, 30)
    checks.append({"title": "Jupyter", "ok": ready_ok,
                   "detail": "работает" if ready_ok else f"не отвечает:\n{lab.log_tail(8)}"})
    if ready_ok:
        probe = ("import sys, pandas, numpy, polars, duckdb, matplotlib\n"
                 "print('Python', sys.version.split()[0], '· pandas', pandas.__version__, '· numpy', numpy.__version__,"
                 " '· polars', polars.__version__, '· duckdb', duckdb.__version__)")
        try:
            result = await kernels.execute("check", probe, timeout=90)
            checks.append({"title": "Ядро Python", "ok": result["status"] == "ok", "detail": result["output"][:500]})
        except (KernelError, PathError, OSError, httpx.HTTPError, websockets.WebSocketException) as exc:
            checks.append({"title": "Ядро Python", "ok": False, "detail": str(exc)[:500]})
        finally:
            try:
                await kernels.close("check")
            except (KernelError, httpx.HTTPError):
                pass
    checks.append({"title": "Лимит памяти контейнера", "ok": True, "detail": _memory_limit()})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
