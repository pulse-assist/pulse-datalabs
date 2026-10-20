"""Ноутбук и вывод ячеек — текстом для агента: код, Markdown, текст вывода, таблицы, ошибки; картинки — пометкой
или файлом."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import Any

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TAGS = re.compile(r"<[^>]+>")
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/svg+xml": "svg", "image/gif": "gif"}
RICH_NOTES = {
    "application/vnd.plotly.v1+json": "график Plotly",
    "application/vnd.vegalite.v5+json": "график Vega-Lite",
    "application/vnd.jupyter.widget-view+json": "виджет",
    "application/vnd.bokehjs_exec.v0+json": "график Bokeh",
}


def source_text(value: Any) -> str:
    return "".join(value) if isinstance(value, list) else str(value or "")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def html_text(html: str) -> str:
    text = TAGS.sub(" ", html.replace("</tr>", "\n").replace("<br>", "\n"))
    return re.sub(r"[ \t]+", " ", text).strip()


def image_bytes(mime: str, value: Any) -> bytes:
    data = source_text(value)
    return data.encode("utf-8") if mime == "image/svg+xml" else base64.b64decode(data)


SaveImage = Callable[[str, bytes], str]     # (расширение, байты) → путь в «Файлах»


def output_text(output: dict, save_image: SaveImage | None = None, traceback_lines: int = 25) -> str:
    kind = output.get("output_type")
    if kind == "stream":
        text = strip_ansi(source_text(output.get("text")))
        return f"[stderr]\n{text}" if output.get("name") == "stderr" and text.strip() else text
    if kind == "error":
        trace = strip_ansi("\n".join(output.get("traceback") or []))
        lines = trace.splitlines()
        if len(lines) > traceback_lines:
            lines = ["…"] + lines[-traceback_lines:]
        return f"Ошибка {output.get('ename')}: {output.get('evalue')}\n" + "\n".join(lines)
    data = output.get("data") or {}
    parts: list[str] = []
    for mime, ext in IMAGE_TYPES.items():
        if mime in data:
            if save_image is not None:
                try:
                    parts.append(f"[картинка: {save_image(ext, image_bytes(mime, data[mime]))}]")
                except (ValueError, OSError) as exc:
                    parts.append(f"[картинка {ext}: не сохранилась — {exc}]")
            else:
                parts.append(f"[картинка {ext}]")
            break
    notes = [note for mime, note in RICH_NOTES.items() if mime in data]
    if parts:
        pass                          # у картинки text/plain — «<Figure size 640x480 …>», агенту не нужен
    elif "text/plain" in data:
        parts.append(source_text(data["text/plain"]))
    elif "text/markdown" in data:
        parts.append(source_text(data["text/markdown"]))
    elif "text/html" in data and not notes:
        parts.append(html_text(source_text(data["text/html"])))
    parts += [f"[{note} — откройте ноутбук в «Файлах»]" for note in notes]
    return "\n".join(part for part in parts if part)


def outputs_text(outputs: list[dict], save_image: SaveImage | None = None) -> str:
    return "\n".join(text for text in (output_text(o, save_image) for o in outputs) if text.strip()).rstrip()


def parse_cells(spec: str | None, total: int) -> range:
    """«3» или «2-7» (номера с 1) → индексы ячеек."""
    if not spec:
        return range(total)
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", str(spec))
    if not match:
        raise ValueError("cells — номер ячейки (3) или диапазон (2-7)")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end < start:
        raise ValueError("cells — номера с 1, диапазон по возрастанию")
    return range(start - 1, min(end, total))


def notebook_text(nb: dict, *, path: str = "", cells: str | None = None, outputs: bool = True,
                  max_chars: int = 20000) -> str:
    items = nb.get("cells") or []
    kernel = ((nb.get("metadata") or {}).get("kernelspec") or {}).get("name") or "python3"
    head = f"# {path or 'ноутбук'} · ядро {kernel} · ячеек: {len(items)}"
    chunks = [head]
    for index in parse_cells(cells, len(items)):
        cell = items[index]
        kind = cell.get("cell_type")
        tags = (cell.get("metadata") or {}).get("tags") or []
        mark = f" [{', '.join(tags)}]" if tags else ""
        if kind == "code":
            count = cell.get("execution_count")
            state = f"выполнена: {count}" if count else "не выполнена"
            chunk = f"[{index + 1}] код ({state}){mark}\n```python\n{source_text(cell.get('source')).rstrip()}\n```"
            if outputs and cell.get("outputs"):
                text = outputs_text(cell["outputs"])
                if text:
                    chunk += f"\n→ вывод:\n{text}"
        elif kind == "markdown":
            chunk = f"[{index + 1}] markdown{mark}\n{source_text(cell.get('source')).rstrip()}"
        else:
            chunk = f"[{index + 1}] {kind}{mark}\n{source_text(cell.get('source')).rstrip()}"
        chunks.append(chunk)
    text = "\n\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n… обрезано на {max_chars} символах: --cells N-M покажет остальное"
    return text


def first_error(nb: dict) -> dict | None:
    """Ячейка, на которой выполнение остановилось: номер, тип ошибки, текст, хвост трейсбэка."""
    for index, cell in enumerate(nb.get("cells") or []):
        for output in cell.get("outputs") or []:
            if output.get("output_type") == "error":
                return {"cell": index + 1, "ename": output.get("ename"), "evalue": output.get("evalue"),
                        "traceback": output_text(output, traceback_lines=12),
                        "source": source_text(cell.get("source"))[:1500]}
    return None
