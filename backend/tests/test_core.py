"""Без Jupyter и Docker: пути, кто может дойти до прокси, ноутбук текстом, сбор вывода ядра, сборка ноутбука."""

import base64
import json
from pathlib import Path

import nbformat
import pytest

from datalabs_plugin import guard
from datalabs_plugin.checkpoints import checkpoint_file
from datalabs_plugin.jupyter import server_config
from datalabs_plugin.kernels import AgentKernels, Collector, KernelError, build_notebook, check_name, execute_request
from datalabs_plugin.nbtext import first_error, notebook_text, output_text, parse_cells
from datalabs_plugin.paths import PathError, Tree, free_name

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def make_tree(tmp_path: Path) -> Tree:
    root = tmp_path / "fs"
    (root / "Продажи").mkdir(parents=True)
    own = root / "Plugins" / "DataLabs"
    own.mkdir(parents=True)
    return Tree(root, "/root/pulse_data/fs", "Plugins/DataLabs", own)


# --- пути -------------------------------------------------------------------------------------------------

def test_tree_resolves_catalog_and_agent_paths(tmp_path):
    tree = make_tree(tmp_path)
    target = tmp_path / "fs" / "Продажи" / "a.ipynb"
    assert tree.resolve("Продажи/a.ipynb") == target
    assert tree.resolve("/Продажи/./a.ipynb") == target
    assert tree.resolve("/root/pulse_data/fs/Продажи/a.ipynb") == target
    assert tree.resolve(str(target)) == target
    assert tree.rel(target) == "Продажи/a.ipynb"
    assert tree.rel(tmp_path / "fs" / "Plugins" / "DataLabs" / "x.png") == "Plugins/DataLabs/x.png"
    with pytest.raises(PathError, match="за пределами"):
        tree.resolve("Продажи/../../etc/passwd")
    with pytest.raises(PathError, match="не ноутбук"):
        tree.notebook("Продажи/a.csv", must_exist=False)
    with pytest.raises(PathError, match="Нет такого"):
        tree.notebook("Продажи/a.ipynb")
    with pytest.raises(PathError, match="files:mount"):
        Tree(None).resolve("a.ipynb")


def test_free_name(tmp_path):
    (tmp_path / "отчёт.html").write_text("x")
    (tmp_path / "отчёт (2).html").write_text("x")
    assert free_name(tmp_path / "отчёт.html").name == "отчёт (3).html"
    assert free_name(tmp_path / "новый.html").name == "новый.html"


def test_checkpoints_live_outside_files():
    path = checkpoint_file("/plugin/data/checkpoints", "checkpoint", "Продажи/Разбор.ipynb")
    assert Path(path).as_posix() == "/plugin/data/checkpoints/Продажи/Разбор-checkpoint.ipynb"
    assert Path(checkpoint_file("/c", "checkpoint", "a.ipynb")).as_posix() == "/c/a-checkpoint.ipynb"


# --- кто может дойти до Jupyter -----------------------------------------------------------------------------

@pytest.mark.parametrize("host, origin, forwarded, public, ok", [
    ("127.0.0.1:41000", None, None, None, True),                                   # WebSocket из Пульса, агенты
    ("127.0.0.1:41000", "https://pulse.example", "pulse.example", None, True),    # браузер владельца через Пульс
    ("127.0.0.1:41000", "https://pulse.example", None, "https://pulse.example/pulse", True),
    ("127.0.0.1:41000", "http://localhost:8001", None, None, True),               # SSH-туннель
    ("127.0.0.1:41000", "https://evil.example", None, "https://pulse.example", False),   # чужой сайт
    ("127.0.0.1:41000", "https://evil.example", "pulse.example", None, False),
    ("127.0.0.1:41000", "null", None, None, False),                                # песочница
    ("evil.example:41000", None, None, None, False),                                # DNS rebinding
    ("[::1]:41000", None, None, None, True),
])
def test_guard(host, origin, forwarded, public, ok):
    assert guard.allowed(host, origin, forwarded, public) is ok


# --- конфиг Jupyter -----------------------------------------------------------------------------------------

def test_server_config_is_python(tmp_path):
    text = server_config(base_url="/pulse/plugin-apps/datalabs/jupyter/", root_dir=tmp_path, port=41001,
                         token="t", data_dir=tmp_path / "data", idle_minutes=60)

    class Config(dict):
        def __getattr__(self, name):
            return self.setdefault(name, Config())

        def __setattr__(self, name, value):
            self[name] = value

    config = Config()
    exec(compile(text, "jupyter_server_config.py", "exec"), {"get_config": lambda: config})
    assert config["ServerApp"]["base_url"] == "/pulse/plugin-apps/datalabs/jupyter/"
    assert config["ServerApp"]["ip"] == "127.0.0.1" and config["ServerApp"]["open_browser"] is False
    assert config["MappingKernelManager"]["cull_idle_timeout"] == 3600
    assert config["ContentsManager"]["checkpoints_class"].endswith("DataCheckpoints")
    assert config["LabApp"]["news_url"] is None


# --- ноутбук текстом ----------------------------------------------------------------------------------------

def sample_notebook() -> dict:
    nb = nbformat.v4.new_notebook()
    nb.cells = [
        nbformat.v4.new_markdown_cell("# Продажи"),
        nbformat.v4.new_code_cell("x = 1", metadata={"tags": ["parameters"]}),
        nbformat.v4.new_code_cell("df.head()", execution_count=2, outputs=[
            nbformat.v4.new_output("stream", name="stdout", text="строк: 3\n"),
            nbformat.v4.new_output("execute_result", data={"text/plain": "   a\n0  1", "text/html": "<table/>"},
                                   execution_count=2),
            nbformat.v4.new_output("display_data", data={"image/png": PNG, "text/plain": "<Figure>"}),
            nbformat.v4.new_output("display_data", data={"application/vnd.plotly.v1+json": {}, "text/html": "<div/>"}),
        ]),
        nbformat.v4.new_code_cell("1/0", execution_count=3, outputs=[nbformat.v4.new_output(
            "error", ename="ZeroDivisionError", evalue="division by zero",
            traceback=["\x1b[31mZeroDivisionError\x1b[0m", "division by zero"])]),
    ]
    return nb


def test_notebook_text():
    text = notebook_text(sample_notebook(), path="Продажи/a.ipynb")
    assert text.startswith("# Продажи/a.ipynb · ядро python3 · ячеек: 4")
    assert "[2] код (не выполнена) [parameters]" in text and "[3] код (выполнена: 2)" in text
    assert "строк: 3" in text and "0  1" in text and "[картинка png]" in text
    assert "график Plotly" in text and "<div/>" not in text
    assert "Ошибка ZeroDivisionError: division by zero" in text and "\x1b[" not in text
    assert "→ вывод" not in notebook_text(sample_notebook(), outputs=False)
    only = notebook_text(sample_notebook(), cells="1-2")
    assert "[1] markdown" in only and "[3]" not in only
    assert "обрезано" in notebook_text(sample_notebook(), max_chars=50)
    with pytest.raises(ValueError):
        parse_cells("5-2", 10)
    assert list(parse_cells("3", 10)) == [2]


def test_first_error_and_saved_image():
    error = first_error(sample_notebook())
    assert error["cell"] == 4 and error["ename"] == "ZeroDivisionError" and "1/0" in error["source"]
    saved = []
    text = output_text({"output_type": "display_data", "data": {"image/png": PNG}},
                       lambda ext, data: saved.append((ext, data)) or "Plugins/DataLabs/1.png")
    assert text == "[картинка: Plugins/DataLabs/1.png]" and saved[0][0] == "png"
    assert saved[0][1].startswith(b"\x89PNG")


# --- живое ядро: сообщения, история, ноутбук ------------------------------------------------------------------

def test_collector_builds_outputs():
    request = execute_request("print(1); 2", "s")
    msg_id = request["header"]["msg_id"]
    assert request["content"]["code"] == "print(1); 2" and request["channel"] == "shell"
    c = Collector(msg_id)

    def msg(kind, content, parent=msg_id, channel="iopub"):
        return {"msg_type": kind, "channel": channel, "parent_header": {"msg_id": parent}, "content": content}

    c.feed(msg("status", {"execution_state": "busy"}))
    c.feed(msg("execute_input", {"execution_count": 7}))
    c.feed(msg("stream", {"name": "stdout", "text": "1"}))
    c.feed(msg("stream", {"name": "stdout", "text": "\n"}))
    c.feed(msg("stream", {"name": "stdout", "text": "чужое"}, parent="other"))
    c.feed(msg("execute_result", {"data": {"text/plain": "2"}, "metadata": {}, "execution_count": 7}))
    assert not c.done
    c.feed(msg("status", {"execution_state": "idle"}))
    c.feed(msg("execute_reply", {"status": "ok", "execution_count": 7}, channel="shell"))
    assert c.done and c.execution_count == 7
    assert c.outputs[0] == {"output_type": "stream", "name": "stdout", "text": "1\n"}
    assert c.outputs[1]["data"] == {"text/plain": "2"}


def test_build_notebook_validates():
    nb = build_notebook([
        {"code": "import pandas as pd", "outputs": [], "status": "ok"},
        {"code": "2", "outputs": [{"output_type": "execute_result", "data": {"text/plain": "2"}, "metadata": {},
                                   "execution_count": 41}], "status": "ok"},
        {"code": "plot()", "outputs": [{"output_type": "display_data", "data": {"image/png": PNG}, "metadata": {}},
                                       {"output_type": "stream", "name": "stderr", "text": "warn"}], "status": "ok"},
    ], "Разбор")
    assert nb.cells[0].cell_type == "markdown" and nb.cells[0].source == "# Разбор"
    assert nb.cells[2].execution_count == 2 and nb.cells[2].outputs[0].execution_count == 2
    nbformat.validate(nb)


def test_save_from_history(tmp_path):
    tree = make_tree(tmp_path)
    kernels = AgentKernels(jupyter=None, tree=tree, store_dir=tmp_path / "agents", images_dir=tree.own_dir)
    with pytest.raises(KernelError, match="нет выполненных"):
        kernels.save("main", "Продажи/r.ipynb")
    kernels._append("main", {"code": "a = 1", "outputs": [], "status": "ok"})
    kernels._append("main", {"code": "1/0", "outputs": [{"output_type": "error", "ename": "E", "evalue": "v",
                                                          "traceback": []}], "status": "error"})
    kernels._append("main", {"code": "a", "outputs": [{"output_type": "execute_result", "data": {"text/plain": "1"},
                                                        "metadata": {}, "execution_count": 3}], "status": "ok"})
    result = kernels.save("main", "/root/pulse_data/fs/Продажи/r.ipynb", title="Итог")
    assert result == {"path": "Продажи/r.ipynb", "cells": 3, "from_session": "main"}
    nb = nbformat.read(str(tmp_path / "fs" / "Продажи" / "r.ipynb"), as_version=4)
    assert [c.source for c in nb.cells] == ["# Итог", "a = 1", "a"]
    with pytest.raises(PathError, match="уже есть"):
        kernels.save("main", "Продажи/r.ipynb")
    assert kernels.save("main", "Продажи/r.ipynb", errors=True, overwrite=True)["cells"] == 3    # без заголовка
    assert check_name(None) == "main"
    with pytest.raises(KernelError):
        check_name("../x")


def test_image_saver_writes_into_plugin_folder(tmp_path):
    tree = make_tree(tmp_path)
    kernels = AgentKernels(jupyter=None, tree=tree, store_dir=tmp_path / "agents", images_dir=tree.own_dir)
    save = kernels._image_saver("main", 4)
    assert save("png", b"x") == "Plugins/DataLabs/агенты/main/ячейка 4-1.png"
    assert save("svg", b"<svg/>") == "Plugins/DataLabs/агенты/main/ячейка 4-2.svg"
    assert json.loads(json.dumps(kernels.history("main"))) == []
