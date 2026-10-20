"""Файлы владельца в контейнере: дерево «Файлов» (право files:mount) и перевод путей.

Агент пишет путь как в «Файлах» («Аналитика/продажи.ipynb») или абсолютный путь своего дерева
($PULSE_FS_DIR/Аналитика/…) — в контейнере это одно и то же место под PULSE_PLUGIN_FS_DIR.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathError(ValueError):
    """Путь вне дерева или не тот файл — понятный текст для агента."""


class Tree:
    def __init__(self, root: Path | None, host_root: str = "", own_folder: str = "", own_dir: Path | None = None):
        self.root = root                     # дерево «Файлов» в процессе плагина (None — права нет)
        self.host_root = host_root.rstrip("/")
        self.own_folder = own_folder.strip("/")    # своя папка плагина в каталоге (Plugins/DataLabs)
        self.own_dir = own_dir                     # она же на диске контейнера

    @classmethod
    def from_env(cls, own_folder: str = "", own_dir: Path | None = None) -> "Tree":
        root = os.environ.get("PULSE_PLUGIN_FS_DIR")
        return cls(Path(root) if root else None, os.environ.get("PULSE_PLUGIN_FS_HOST_DIR", ""), own_folder, own_dir)

    def require(self) -> Path:
        if self.root is None:
            raise PathError("Нет дерева «Файлов» (право files:mount): обновите Пульс до контракта плагинов 1.11")
        return self.root

    def available(self) -> bool:
        """Дерево читается: после перезапуска Пульса монтирование может умереть (Transport endpoint is not connected)."""
        if self.root is None:
            return False
        try:
            os.listdir(self.root)
            return True
        except OSError:
            return False

    def resolve(self, path: str) -> Path:
        """Путь «Файлов» или агента → путь в контейнере. «..» и выход за дерево — ошибка."""
        root = self.require()
        value = str(path or "").strip().replace("\\", "/")
        if not value:
            raise PathError("Пустой путь")
        for prefix in (self.host_root, root.as_posix().rstrip("/")):
            if prefix and (value == prefix or value.startswith(prefix + "/")):
                value = value[len(prefix):]
                break
        parts = [part for part in value.split("/") if part not in ("", ".")]
        if ".." in parts:
            raise PathError(f"Путь за пределами «Файлов»: {path}")
        return root.joinpath(*parts)

    def rel(self, path: Path) -> str:
        """Путь в контейнере → путь в «Файлах» (для ответов и ссылок)."""
        if self.own_dir is not None:
            try:
                rest = path.relative_to(self.own_dir).as_posix()
                return f"{self.own_folder}/{rest}".strip("/") if rest != "." else self.own_folder
            except ValueError:
                pass
        root = self.require()
        try:
            rest = path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
        return "" if rest == "." else rest

    def notebook(self, path: str, *, must_exist: bool = True) -> Path:
        target = self.resolve(path)
        if target.suffix.lower() != ".ipynb":
            raise PathError(f"Это не ноутбук (.ipynb): {path}")
        if must_exist and not target.is_file():
            raise PathError(f"Нет такого ноутбука: {path}")
        return target


def free_name(target: Path) -> Path:
    """Имя без перезаписи: «отчёт.html» → «отчёт (2).html», как у «Файлов»."""
    if not target.exists():
        return target
    n = 2
    while True:
        candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not candidate.exists():
            return candidate
        n += 1
