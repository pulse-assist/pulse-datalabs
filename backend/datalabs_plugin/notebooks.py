"""Ноутбук целиком: прочитать текстом, выполнить (papermill), выгрузить (nbconvert), собрать отчёт.

papermill и nbconvert запускаются отдельными процессами в своей группе: зависший ноутбук убивается вместе с ядром.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import nbformat

from .jupyter import kernel_env
from .nbtext import first_error, notebook_text, outputs_text
from .paths import PathError, Tree, free_name

FORMATS = {"html": ("html", ".html"), "pdf": ("webpdf", ".pdf"), "py": ("script", ".py"), "md": ("markdown", ".md")}
PREVIEW_CHARS = 3000


class RunError(Exception):
    pass


def parse_params(value: Any) -> dict:
    if value in (None, "", True):
        return {}
    if isinstance(value, dict):
        return value
    try:
        params = json.loads(str(value))
    except ValueError as exc:
        raise RunError(f"params — JSON-объект, например {{\"month\": \"2026-10\"}}: {exc}") from None
    if not isinstance(params, dict):
        raise RunError("params — JSON-объект {\"имя\": значение}")
    return params


def flag(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "да", "on")


def files_link(path: str) -> str:
    return "/files?path=" + quote(path, safe="/@")


def _run_process(argv: list[str], *, cwd: Path, env: dict, timeout: float) -> tuple[int, str, bool]:
    """Процесс в своей группе: по таймауту убиваем всю группу (papermill + ядро). → (код, вывод, таймаут)."""
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            proc.kill()
        out, _ = proc.communicate()
        return -9, out.decode("utf-8", "replace"), True


def trust(nb) -> None:
    """Ноутбук, который собрал или выполнил плагин, — доверенный: Jupyter покажет его HTML и JS (Plotly, виджеты).
    Подпись — в базе Jupyter (JUPYTER_DATA_DIR в данных плагина), общей с Jupyter Server; не вышло — не страшно."""
    try:
        from nbformat.sign import NotebookNotary

        NotebookNotary().sign(nb)
    except Exception:  # noqa: BLE001 — нет каталога данных, sqlite занят
        pass


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(line for line in text.splitlines()[-lines:])


class Notebooks:
    def __init__(self, tree: Tree, timeout_minutes: Callable[[], int] = lambda: 30):
        self.tree = tree
        self.timeout_minutes = timeout_minutes

    def read(self, path: str, cells: str | None = None, outputs: bool = True, max_chars: int = 20000) -> str:
        target = self.tree.notebook(path)
        try:
            nb = nbformat.read(str(target), as_version=4)
        except Exception as exc:  # noqa: BLE001 — битый JSON, не ноутбук
            raise RunError(f"Не читается как ноутбук: {exc}") from None
        return notebook_text(nb, path=self.tree.rel(target), cells=cells, outputs=outputs, max_chars=max_chars)

    def run(self, path: str, params: Any = None, output: str | None = None) -> dict:
        source = self.tree.notebook(path)
        target = self.tree.notebook(output, must_exist=False) if output else source
        return self._papermill(source, target, parse_params(params))

    def _papermill(self, source: Path, target: Path, params: dict) -> dict:
        target.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, "-m", "papermill", str(source), str(target), "--cwd", str(source.parent),
                "-k", "python3", "--request-save-on-cell-execute", "--no-progress-bar", "--log-level", "WARNING"]
        if params:
            argv += ["-y", json.dumps(params, ensure_ascii=False)]
        limit = max(1, int(self.timeout_minutes() or 30)) * 60
        started = time.monotonic()
        code, out, timed_out = _run_process(argv, cwd=source.parent, env=kernel_env(self.tree.require()),
                                            timeout=limit)
        result: dict[str, Any] = {"ok": False, "path": self.tree.rel(target),
                                  "seconds": round(time.monotonic() - started, 1)}
        nb = None
        if target.is_file():
            try:
                nb = nbformat.read(str(target), as_version=4)
            except Exception:  # noqa: BLE001 — papermill не успел записать
                nb = None
        if nb is not None:
            trust(nb)
        error = first_error(nb) if nb is not None else None
        if timed_out:
            result["error"] = {"message": f"не уложился в {limit // 60} мин (настройка run_timeout_minutes) — прерван"}
        elif error:
            result["error"] = error
        elif code != 0:
            result["error"] = {"message": f"papermill завершился с кодом {code}", "log": _tail(out)}
        else:
            result["ok"] = True
        if nb is not None:
            result["cells"] = len(nb.cells)
            last = next((c for c in reversed(nb.cells) if c.cell_type == "code" and c.get("outputs")), None)
            if last is not None and result["ok"]:
                preview = outputs_text(last.outputs)
                result["last_output"] = preview[:PREVIEW_CHARS] + ("…" if len(preview) > PREVIEW_CHARS else "")
        return result

    def export(self, path: str, to: str | None = "html", output: str | None = None, no_input: bool = False) -> dict:
        source = self.tree.notebook(path)
        fmt = (to or "html").strip().lower()
        if fmt not in FORMATS:
            raise RunError(f"to — {', '.join(FORMATS)}")
        exporter, suffix = FORMATS[fmt]
        if output:
            target = self.tree.resolve(output)
            if target.suffix.lower() != suffix:
                target = target / (source.stem + suffix)
        else:
            target = source.with_suffix(suffix)
        return self._nbconvert(source, target, exporter, no_input)

    def _nbconvert(self, source: Path, target: Path, exporter: str, no_input: bool) -> dict:
        target.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, "-m", "jupyter", "nbconvert", "--to", exporter, str(source),
                "--output-dir", str(target.parent), "--output", target.stem]
        if no_input:
            argv.append("--no-input")
        code, out, timed_out = _run_process(argv, cwd=source.parent, env=kernel_env(self.tree.require()),
                                            timeout=540)
        if timed_out or code != 0:
            raise RunError(f"Выгрузка не удалась ({'таймаут' if timed_out else f'код {code}'}): {_tail(out, 8)}")
        produced = next((p for p in target.parent.glob(target.stem + ".*")
                         if p.suffix.lower() == target.suffix.lower()), target)
        return {"path": self.tree.rel(produced), "format": exporter}

    def report(self, path: str, *, params: Any = None, to: str | None = None, fmt: str | None = "html",
               code: bool = False, tz: str = "UTC", now: datetime | None = None) -> dict:
        source = self.tree.notebook(path)
        fmt = (fmt or "html").strip().lower()
        if fmt not in ("html", "pdf"):
            raise RunError("format — html или pdf")
        folder = self.tree.resolve(to) if to else source.parent / "Отчёты"
        if folder.exists() and not folder.is_dir():
            raise PathError(f"Это не папка: {to}")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            zone = ZoneInfo(tz)
        except Exception:  # noqa: BLE001 — неизвестный пояс
            zone = ZoneInfo("UTC")
        stamp = (now or datetime.now(zone)).strftime("%Y-%m-%d %H-%M")
        executed = free_name(folder / f"{source.stem} {stamp}.ipynb")
        result = self._papermill(source, executed, parse_params(params))
        result["notebook"] = result.pop("path")
        if result["ok"]:
            exporter, suffix = FORMATS[fmt]
            result["report"] = self._nbconvert(executed, executed.with_suffix(suffix), exporter, not code)["path"]
        return result


def report_message(result: dict, title: str) -> str:
    """Уведомление владельцу: ссылки — относительные, Telegram получит их абсолютными (внешний адрес Пульса)."""
    notebook = result.get("notebook", "")
    if result.get("ok"):
        report = result["report"]
        return (f"Отчёт «{title}» готов: [{Path(report).name}]({files_link(report)}) · "
                f"[ноутбук]({files_link(notebook)}) · {result.get('seconds', 0):.0f} с")
    error = result.get("error") or {}
    if error.get("cell"):
        what = f"ячейка {error['cell']}: {error.get('ename')}: {error.get('evalue')}"
    else:
        what = error.get("message") or "ошибка выполнения"
    link = f" [Ноутбук с ошибкой]({files_link(notebook)})" if notebook else ""
    return f"Отчёт «{title}» не собрался — {what}.{link}"
