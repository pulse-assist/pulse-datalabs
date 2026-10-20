"""Контрольные точки Jupyter — в данных плагина, а не папками .ipynb_checkpoints по всем «Файлам» владельца."""

from __future__ import annotations

import os

from jupyter_server.services.contents.filecheckpoints import AsyncFileCheckpoints
from traitlets import Unicode


def checkpoint_file(root: str, checkpoint_id: str, path: str) -> str:
    """<root>/<папка ноутбука>/<имя>-<id><расширение> — та же раскладка, что у дерева «Файлов»."""
    parent, _, name = path.strip("/").rpartition("/")
    stem, ext = os.path.splitext(name)
    directory = os.path.join(root, *[part for part in parent.split("/") if part])
    return os.path.join(directory, f"{stem}-{checkpoint_id}{ext}")


class DataCheckpoints(AsyncFileCheckpoints):
    root = Unicode("/plugin/data/checkpoints", config=True, help="Куда складывать контрольные точки")

    def checkpoint_path(self, checkpoint_id, path):
        target = checkpoint_file(self.root, checkpoint_id, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return target
