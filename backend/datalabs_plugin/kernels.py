"""Живые ядра агентов: код уходит в ядро Jupyter (сессия «агент: <имя>»), вывод собирается до idle, картинки —
файлами в папку плагина, каждая ячейка — в историю, из которой `save` собирает ноутбук.

Ядра — обычные ядра Jupyter Server: владелец видит их в JupyterLab («Работающие»), простаивающие останавливает
Jupyter (настройка kernel_idle_minutes).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import nbformat
import websockets

from .jupyter import Jupyter
from .nbtext import outputs_text
from .notebooks import trust
from .paths import PathError, Tree

NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
EXEC_TIMEOUT = 300
EXEC_TIMEOUT_MAX = 840          # команда ждёт 900 с: остаётся время прервать и ответить
MAX_TEXT = 20000


class KernelError(Exception):
    pass


def check_name(name: str | None) -> str:
    value = (name or "main").strip().lower()
    if not value or len(value) > 40 or set(value) - NAME_OK:
        raise KernelError("session — латиница, цифры, «-» и «_», до 40 символов")
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Collector:
    """Сообщения iopub/shell одного execute_request → выходы в формате nbformat."""

    def __init__(self, msg_id: str):
        self.msg_id = msg_id
        self.outputs: list[dict] = []
        self.execution_count: int | None = None
        self.reply: dict | None = None
        self.idle = False

    @property
    def done(self) -> bool:
        return self.idle and self.reply is not None

    def feed(self, message: dict) -> None:
        if (message.get("parent_header") or {}).get("msg_id") != self.msg_id:
            return
        kind = message.get("msg_type") or (message.get("header") or {}).get("msg_type")
        content = message.get("content") or {}
        if kind == "status":
            self.idle = self.idle or content.get("execution_state") == "idle"
        elif kind == "execute_input":
            self.execution_count = content.get("execution_count")
        elif kind == "stream":
            last = self.outputs[-1] if self.outputs else None
            if last and last.get("output_type") == "stream" and last.get("name") == content.get("name"):
                last["text"] += content.get("text", "")
            else:
                self.outputs.append({"output_type": "stream", "name": content.get("name", "stdout"),
                                     "text": content.get("text", "")})
        elif kind in ("execute_result", "display_data"):
            output = {"output_type": kind, "data": content.get("data") or {}, "metadata": content.get("metadata") or {}}
            if kind == "execute_result":
                output["execution_count"] = content.get("execution_count")
            self.outputs.append(output)
        elif kind == "error":
            self.outputs.append({"output_type": "error", "ename": content.get("ename", ""),
                                 "evalue": content.get("evalue", ""), "traceback": content.get("traceback") or []})
        elif kind == "clear_output":
            self.outputs.clear()
        elif kind == "execute_reply":
            self.reply = content
            self.execution_count = content.get("execution_count", self.execution_count)


def execute_request(code: str, session: str) -> dict:
    msg_id = uuid.uuid4().hex
    return {
        "header": {"msg_id": msg_id, "username": "agent", "session": session, "msg_type": "execute_request",
                   "version": "5.3", "date": now_iso()},
        "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
        "content": {"code": code, "silent": False, "store_history": True, "user_expressions": {},
                    "allow_stdin": False, "stop_on_error": True},
    }


class AgentKernels:
    def __init__(self, jupyter: Jupyter, tree: Tree, store_dir: Path, images_dir: Path):
        self.jupyter = jupyter
        self.tree = tree
        self.store_dir = store_dir
        self.images_dir = images_dir
        self._locks: dict[str, asyncio.Lock] = {}

    # --- состояние ------------------------------------------------------------------------------------------

    @property
    def _state_file(self) -> Path:
        return self.store_dir / "sessions.json"

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, dict]) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._state_file)

    def _history_file(self, name: str) -> Path:
        return self.store_dir / f"{name}.jsonl"

    def history(self, name: str) -> list[dict]:
        try:
            lines = self._history_file(name).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def _append(self, name: str, item: dict) -> int:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        with self._history_file(name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        return len(self.history(name))

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self.jupyter.headers, timeout=30)

    # --- ядро -----------------------------------------------------------------------------------------------

    async def _kernel_alive(self, client: httpx.AsyncClient, kernel_id: str) -> bool:
        response = await client.get(self.jupyter.url(f"api/kernels/{kernel_id}"))
        return response.status_code == 200

    async def _ensure(self, name: str, cwd: str | None) -> tuple[dict, bool]:
        """Ядро сессии агента: живое — как есть, нет (остановлено за простоем, перезапуск плагина) — новое."""
        state = self._load()
        info = state.get(name)
        async with self._client() as client:
            if info and await self._kernel_alive(client, info["kernel_id"]):
                return info, False
            folder = ""
            if cwd:
                target = self.tree.resolve(cwd)
                if not target.is_dir():
                    raise PathError(f"Нет такой папки: {cwd}")
                folder = self.tree.rel(target)
            elif info:
                folder = info.get("cwd", "")
            response = await client.post(self.jupyter.url("api/sessions"), json={
                "path": f"{folder}/агент-{name}".lstrip("/"), "name": f"агент: {name}", "type": "console",
                "kernel": {"name": "python3"}})
            if response.status_code >= 400:
                raise KernelError(f"Ядро не запустилось: HTTP {response.status_code} {response.text[:300]}")
            body = response.json()
        restarted = info is not None
        info = {"session_id": body["id"], "kernel_id": body["kernel"]["id"], "cwd": folder,
                "created_at": now_iso(), "last_at": now_iso()}
        state[name] = info
        self._save(state)
        if restarted:
            self._history_file(name).unlink(missing_ok=True)
        return info, restarted

    async def _interrupt(self, kernel_id: str) -> None:
        async with self._client() as client:
            await client.post(self.jupyter.url(f"api/kernels/{kernel_id}/interrupt"))

    async def _run(self, kernel_id: str, code: str, timeout: float) -> tuple[Collector, bool]:
        session = uuid.uuid4().hex
        request = execute_request(code, session)
        collector = Collector(request["header"]["msg_id"])
        url = self.jupyter.ws_url(f"api/kernels/{kernel_id}/channels?session_id={session}")
        timed_out = False
        async with websockets.connect(url, additional_headers=self.jupyter.headers, max_size=64 * 1024 * 1024,
                                      open_timeout=30) as ws:
            await ws.send(json.dumps(request))
            deadline = time.monotonic() + timeout
            while not collector.done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if timed_out:
                        break                     # и после прерывания не освободилось — отвечаем как есть
                    timed_out = True
                    await self._interrupt(kernel_id)
                    deadline = time.monotonic() + 15
                    continue
                try:
                    raw = await asyncio.wait_for(ws.recv(), remaining)
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    continue                      # сообщения с бинарными буферами (виджеты) агенту не нужны
                collector.feed(json.loads(raw))
        return collector, timed_out

    def _image_saver(self, name: str, cell: int):
        folder = self.images_dir / "агенты" / name
        counter = iter(range(1, 1000))

        def save(ext: str, data: bytes) -> str:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"ячейка {cell}-{next(counter)}.{ext}"
            target.write_bytes(data)
            return self.tree.rel(target)

        return save

    async def execute(self, name: str | None, code: str, cwd: str | None = None,
                      timeout: float | None = None) -> dict[str, Any]:
        name = check_name(name)
        if not code.strip():
            raise KernelError("Пустой код: --code \"…\" или --code_file путь")
        timeout = min(float(timeout or EXEC_TIMEOUT), EXEC_TIMEOUT_MAX)
        lock = self._locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise KernelError(f"Ядро {name} ещё выполняет прошлую ячейку — дождитесь или возьмите другое --session")
        async with lock:
            info, restarted = await self._ensure(name, cwd)
            started = time.monotonic()
            collector, timed_out = await self._run(info["kernel_id"], code, timeout)
            status = "timeout" if timed_out else ((collector.reply or {}).get("status") or "ok")
            cell = len(self.history(name)) + 1
            text = outputs_text(collector.outputs, self._image_saver(name, cell))
            self._append(name, {"code": code, "outputs": collector.outputs, "execution_count":
                                collector.execution_count, "status": status, "at": now_iso()})
            state = self._load()
            if name in state:
                state[name]["last_at"] = now_iso()
                self._save(state)
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + f"\n… вывод обрезан на {MAX_TEXT} символах: печатайте меньше (head(), describe())"
        result: dict[str, Any] = {"session": name, "cell": cell, "status": status,
                                  "seconds": round(time.monotonic() - started, 1), "output": text}
        if restarted:
            result["note"] = "ядро было остановлено (простой или перезапуск) и запущено заново: прежних переменных нет"
        if timed_out:
            result["note"] = f"не уложилось в {int(timeout)} с — выполнение прервано (KeyboardInterrupt)"
        return result

    # --- управление -----------------------------------------------------------------------------------------

    async def sessions(self) -> list[dict]:
        state = self._load()
        async with self._client() as client:
            response = await client.get(self.jupyter.url("api/kernels"))
            kernels = {k["id"]: k for k in response.json()} if response.status_code == 200 else {}
        return [{"session": name, "cwd": info.get("cwd") or "/", "cells": len(self.history(name)),
                 "alive": info["kernel_id"] in kernels,
                 "state": (kernels.get(info["kernel_id"]) or {}).get("execution_state"),
                 "last_at": info.get("last_at")} for name, info in sorted(state.items())]

    async def reset(self, name: str | None) -> dict:
        name = check_name(name)
        info = self._load().get(name)
        if not info:
            raise KernelError(f"Нет ядра {name}")
        async with self._client() as client:
            if await self._kernel_alive(client, info["kernel_id"]):
                await client.post(self.jupyter.url(f"api/kernels/{info['kernel_id']}/restart"))
        self._history_file(name).unlink(missing_ok=True)
        return {"session": name, "reset": True}

    async def close(self, name: str | None) -> dict:
        name = check_name(name)
        state = self._load()
        info = state.pop(name, None)
        if not info:
            raise KernelError(f"Нет ядра {name}")
        async with self._client() as client:
            await client.delete(self.jupyter.url(f"api/sessions/{info['session_id']}"))
        self._save(state)
        self._history_file(name).unlink(missing_ok=True)
        return {"session": name, "closed": True}

    def save(self, name: str | None, path: str, *, title: str | None = None, errors: bool = False,
             overwrite: bool = False) -> dict:
        name = check_name(name)
        cells = [item for item in self.history(name) if errors or item.get("status") == "ok"]
        if not cells:
            raise KernelError(f"В ядре {name} нет выполненных без ошибок ячеек — сначала pulse datalabs exec")
        target = self.tree.notebook(path, must_exist=False)
        if target.exists() and not overwrite:
            raise PathError(f"Файл уже есть: {path} — другое имя или --overwrite true")
        nb = build_notebook(cells, title)
        target.parent.mkdir(parents=True, exist_ok=True)
        nbformat.write(nb, str(target))
        trust(nb)
        return {"path": self.tree.rel(target), "cells": len(nb.cells), "from_session": name}


def build_notebook(items: list[dict], title: str | None = None) -> nbformat.NotebookNode:
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3 (ipykernel)", "language": "python"}
    nb.metadata["language_info"] = {"name": "python"}
    if title:
        nb.cells.append(nbformat.v4.new_markdown_cell(f"# {title.strip()}"))
    for number, item in enumerate(items, start=1):
        outputs = []
        for output in item.get("outputs") or []:
            output = dict(output)
            if output["output_type"] == "execute_result":
                output["execution_count"] = number      # ячейки ноутбука нумеруются заново
            outputs.append(nbformat.v4.new_output(**output))
        nb.cells.append(nbformat.v4.new_code_cell(item["code"], outputs=outputs, execution_count=number))
    nbformat.validate(nb)
    return nb
