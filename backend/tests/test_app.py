"""Сервер плагина и запуск ноутбуков без Jupyter: papermill и nbconvert подменены."""

import importlib
import json
from datetime import datetime
from pathlib import Path

import nbformat
import pytest
from fastapi.testclient import TestClient

from datalabs_plugin import notebooks as nbmod
from datalabs_plugin.notebooks import Notebooks, RunError, files_link, parse_params, report_message
from tests.test_core import make_tree, sample_notebook


def write_nb(path: Path, nb=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb or nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("1")]), str(path))
    return path


def fake_papermill(fail: bool = False, calls: list | None = None):
    def run(argv, *, cwd, env, timeout):
        if calls is not None:
            calls.append(argv)
        if argv[2] == "papermill":
            source, target = argv[3], argv[4]
            nb = sample_notebook() if fail else nbformat.read(source, as_version=4)
            if not fail:
                nb.cells[-1].outputs = [nbformat.v4.new_output("stream", name="stdout", text="готово")]
            nbformat.write(nb, target)
            return (1 if fail else 0), "", False
        # nbconvert: --output-dir D --output stem
        out_dir, stem = Path(argv[argv.index("--output-dir") + 1]), argv[argv.index("--output") + 1]
        suffix = {"html": ".html", "webpdf": ".pdf", "script": ".py", "markdown": ".md"}[argv[argv.index("--to") + 1]]
        (out_dir / f"{stem}{suffix}").write_text("<html/>", encoding="utf-8")
        return 0, "", False
    return run


def test_params():
    assert parse_params('{"month": "2026-10"}') == {"month": "2026-10"}
    assert parse_params(None) == {} and parse_params(True) == {}
    with pytest.raises(RunError):
        parse_params("[1]")
    with pytest.raises(RunError):
        parse_params("month=10")
    assert files_link("Отчёты/a b.html") == "/files?path=%D0%9E%D1%82%D1%87%D1%91%D1%82%D1%8B/a%20b.html"


def test_run_ok_and_error(tmp_path, monkeypatch):
    tree = make_tree(tmp_path)
    write_nb(tmp_path / "fs" / "Продажи" / "w.ipynb")
    calls: list = []
    monkeypatch.setattr(nbmod, "_run_process", fake_papermill(calls=calls))
    result = Notebooks(tree).run("Продажи/w.ipynb", '{"weeks": 2}')
    assert result["ok"] and result["path"] == "Продажи/w.ipynb" and result["last_output"] == "готово"
    argv = calls[0]
    assert argv[argv.index("-y") + 1] == '{"weeks": 2}' and argv[argv.index("--cwd") + 1].endswith("Продажи")

    monkeypatch.setattr(nbmod, "_run_process", fake_papermill(fail=True))
    failed = Notebooks(tree).run("Продажи/w.ipynb", output="Продажи/out.ipynb")
    assert not failed["ok"] and failed["error"]["cell"] == 4 and failed["path"] == "Продажи/out.ipynb"


def test_export_targets(tmp_path, monkeypatch):
    tree = make_tree(tmp_path)
    write_nb(tmp_path / "fs" / "Продажи" / "w.ipynb")
    monkeypatch.setattr(nbmod, "_run_process", fake_papermill())
    nbs = Notebooks(tree)
    assert nbs.export("Продажи/w.ipynb")["path"] == "Продажи/w.html"
    assert nbs.export("Продажи/w.ipynb", "pdf", "Отчёты")["path"] == "Отчёты/w.pdf"
    assert nbs.export("Продажи/w.ipynb", "py", "Код/скрипт.py")["path"] == "Код/скрипт.py"
    with pytest.raises(RunError):
        nbs.export("Продажи/w.ipynb", "docx")


def test_report_copy_and_message(tmp_path, monkeypatch):
    tree = make_tree(tmp_path)
    template = write_nb(tmp_path / "fs" / "Продажи" / "Неделя.ipynb")
    before = template.read_bytes()
    monkeypatch.setattr(nbmod, "_run_process", fake_papermill())
    now = datetime(2026, 10, 21, 9, 0)
    result = Notebooks(tree).report("Продажи/Неделя.ipynb", tz="Europe/Moscow", now=now)
    assert result["ok"] and result["notebook"] == "Продажи/Отчёты/Неделя 2026-10-21 09-00.ipynb"
    assert result["report"] == "Продажи/Отчёты/Неделя 2026-10-21 09-00.html"
    assert template.read_bytes() == before                      # шаблон не трогаем
    again = Notebooks(tree).report("Продажи/Неделя.ipynb", now=now)
    assert again["notebook"].endswith("Неделя 2026-10-21 09-00 (2).ipynb")
    message = report_message(result, "Неделя")
    assert message.startswith("Отчёт «Неделя» готов: [Неделя 2026-10-21 09-00.html](/files?path=")

    monkeypatch.setattr(nbmod, "_run_process", fake_papermill(fail=True))
    failed = Notebooks(tree).report("Продажи/Неделя.ipynb", to="Архив", now=now)
    assert not failed["ok"] and "report" not in failed and failed["notebook"].startswith("Архив/")
    assert "ячейка 4: ZeroDivisionError" in report_message(failed, "Неделя")


@pytest.fixture
def client(tmp_path, monkeypatch):
    make_tree(tmp_path)
    write_nb(tmp_path / "fs" / "Продажи" / "w.ipynb", sample_notebook())
    monkeypatch.setenv("PULSE_PLUGIN_FS_DIR", str(tmp_path / "fs"))
    monkeypatch.setenv("PULSE_PLUGIN_FS_HOST_DIR", "/root/pulse_data/fs")
    monkeypatch.setenv("PULSE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PULSE_PLUGIN_FILES", str(tmp_path / "fs" / "Plugins" / "DataLabs"))
    monkeypatch.setenv("PULSE_PLUGIN_FOLDER", "Plugins/DataLabs")
    monkeypatch.setenv("PULSE_PLUGIN_APP_PATH", "/pulse/plugin-apps/datalabs")
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "pyuser"))
    import datalabs_plugin.app as app_module
    module = importlib.reload(app_module)
    monkeypatch.setattr(module.lab, "start", lambda: None)
    monkeypatch.setattr(module.lab, "stop", lambda: None)
    monkeypatch.setattr(module.lab, "ensure", lambda: None)
    with TestClient(module.app, base_url="http://127.0.0.1:41000") as test_client:
        test_client.module = module
        yield test_client


def test_ready_and_guard(client):
    assert client.get("/ready").json()["ok"] is True
    assert client.get("/lab").status_code == 200
    evil = client.get("/lab", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    rebinding = TestClient(client.module.app, base_url="http://evil.example:41000").get("/jupyter/api/status")
    assert rebinding.status_code == 403
    page = client.get("/lab").text
    assert "/pulse/plugin-apps/datalabs/jupyter/lab" in page and 'fetch("theme"' in page


def test_commands_read_and_errors(client):
    response = client.post("/commands/read", json={"path": "/root/pulse_data/fs/Продажи/w.ipynb", "cells": "3"})
    assert response.status_code == 200
    assert "[3] код (выполнена: 2)" in response.json()
    missing = client.post("/commands/read", json={"path": "Продажи/нет.ipynb"})
    assert missing.status_code == 400 and "Нет такого ноутбука" in missing.json()["detail"]
    assert client.post("/commands/run", json={}).status_code == 400
    bad = client.post("/commands/exec", json={"code": "1", "session": "../x"})
    assert bad.status_code == 400 and "латиница" in bad.json()["detail"]


def test_open_file_redirects_to_notebook(client, monkeypatch):
    module = client.module

    class Ticket:
        def meta(self):
            return {"path": "Продажи/w.ipynb"}

        def read(self):
            return nbformat.writes(sample_notebook()).encode()

    monkeypatch.setattr(module.plugin, "file_ticket", lambda ticket: Ticket())
    page = client.get("/open", params={"ticket": "t1"}).text
    assert json.dumps("/pulse/plugin-apps/datalabs/jupyter/notebooks/%D0%9F%D1%80%D0%BE%D0%B4%D0%B0%D0%B6%D0%B8/w.ipynb") \
        in page
    view = client.get("/open", params={"ticket": "t1", "mode": "view"}).text
    assert "view?ticket=t1" in view
