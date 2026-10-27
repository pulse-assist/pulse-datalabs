"""Jupyter Server внутри контейнера плагина: JupyterLab, Notebook 7, ядра, терминал.

Слушает только 127.0.0.1 контейнера; наружу его отдаёт прокси плагина (/jupyter/…) за сессией владельца
(/plugin-apps/datalabs/…). Прокси подставляет токен — в браузер он не уходит. base_url — внешний путь
(<base_path>/plugin-apps/datalabs/jupyter/), чтобы ссылки страниц Jupyter вели куда надо.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("datalabs.jupyter")

THEMES = {"light": "JupyterLab Light", "dark": "JupyterLab Dark"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def kernel_env(root_dir: Path) -> dict[str, str]:
    """Окружение Jupyter, ядер и papermill: без токена и секретов плагина — код ноутбука их не увидит.
    PULSE_FILES — корень «Файлов» владельца (pd.read_csv(f"{os.environ['PULSE_FILES']}/…"))."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("PULSE_PLUGIN_") or key in ("PULSE_PLUGIN_FS_DIR", "PULSE_PLUGIN_FS_HOST_DIR")}
    env["PULSE_FILES"] = str(root_dir)
    return env


KERNEL_NICE = 10


def write_kernelspec(data_dir: Path) -> Path:
    """Ядро python3 с пониженным приоритетом (nice): расчёт ноутбука занимает весь CPU контейнера, а сервер
    плагина и Jupyter должны отвечать — иначе Пульс сочтёт плагин зависшим. Спецификация лежит в JUPYTER_DATA_DIR
    (раньше системной) — её берут и Jupyter, и papermill."""
    target = data_dir / "kernels" / "python3"
    target.mkdir(parents=True, exist_ok=True)
    spec = {"argv": ["nice", "-n", str(KERNEL_NICE), sys.executable, "-m", "ipykernel_launcher",
                     "-f", "{connection_file}"],
            "display_name": "Python 3 (ipykernel)", "language": "python", "metadata": {"debugger": True}}
    (target / "kernel.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


def server_config(*, base_url: str, root_dir: Path, port: int, token: str, data_dir: Path,
                  idle_minutes: int) -> str:
    """jupyter_server_config.py: значения — литералы Python (repr)."""
    values = {
        "ServerApp.ip": "127.0.0.1",
        "ServerApp.port": port,
        "ServerApp.port_retries": 0,
        "ServerApp.open_browser": False,
        "ServerApp.allow_root": True,
        "ServerApp.base_url": base_url,
        "ServerApp.root_dir": str(root_dir),
        "ServerApp.default_url": "/lab",
        # до Jupyter доходят только запросы прокси плагина: Host и Origin он проверяет сам (guard.py)
        "ServerApp.allow_origin": "*",
        "ServerApp.allow_remote_access": True,
        "ServerApp.trust_xheaders": False,
        "ServerApp.terminals_enabled": True,
        "IdentityProvider.token": token,
        "ContentsManager.checkpoints_class": "datalabs_plugin.checkpoints.DataCheckpoints",
        "DataCheckpoints.root": str(data_dir / "checkpoints"),
        "FileContentsManager.delete_to_trash": False,     # корзины в дереве «Файлов» нет
        "FileContentsManager.always_delete_dir": True,
        "MappingKernelManager.cull_idle_timeout": max(0, idle_minutes) * 60,
        "MappingKernelManager.cull_interval": 300,
        "MappingKernelManager.cull_connected": False,
        "LabApp.check_for_updates_class": "jupyterlab.NeverCheckForUpdate",
        "LabApp.news_url": None,
    }
    lines = ["# создаётся плагином DataLabs при каждом запуске — не правьте", "c = get_config()  # noqa"]
    lines += [f"c.{key} = {value!r}" for key, value in values.items()]
    return "\n".join(lines) + "\n"


class Jupyter:
    def __init__(self, *, base_url: str, root_dir: Path, data_dir: Path, idle_minutes: int = 60):
        self.base_url = "/" + base_url.strip("/") + "/"
        self.root_dir = root_dir
        self.data_dir = data_dir / "jupyter"
        self.idle_minutes = idle_minutes
        self.port = free_port()
        self.token = secrets.token_urlsafe(32)
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.restarts = 0
        self._lock = threading.Lock()

    # --- адреса ---------------------------------------------------------------------------------------------

    def url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.port}{self.base_url}{path.lstrip('/')}"

    def ws_url(self, path: str = "") -> str:
        return self.url(path).replace("http://", "ws://", 1)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"}

    # --- процесс --------------------------------------------------------------------------------------------

    def env(self) -> dict[str, str]:
        data = self.data_dir
        env = kernel_env(self.root_dir)
        env.update({
            "JUPYTER_CONFIG_DIR": str(data / "config"),
            "JUPYTER_RUNTIME_DIR": "/tmp/jupyter-runtime",
            "JUPYTERLAB_SETTINGS_DIR": str(data / "lab" / "user-settings"),
            "JUPYTERLAB_WORKSPACES_DIR": str(data / "lab" / "workspaces"),
            "IPYTHONDIR": str(data / "ipython"),
            # рабочая папка Jupyter — «Файлы»: класс контрольных точек ищется по пути кода плагина
            "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent),
                                                       os.environ.get("PYTHONPATH", "")])),
        })
        return env

    def start(self) -> None:
        with self._lock:
            if self.alive():
                return
            (self.data_dir / "config").mkdir(parents=True, exist_ok=True)
            write_kernelspec(Path(os.environ.get("JUPYTER_DATA_DIR") or self.data_dir / "data"))
            config = self.data_dir / "config" / "jupyter_server_config.py"
            config.write_text(server_config(base_url=self.base_url, root_dir=self.root_dir, port=self.port,
                                            token=self.token, data_dir=self.data_dir,
                                            idle_minutes=self.idle_minutes), encoding="utf-8")
            config.chmod(0o600)
            log_file = open(self.data_dir / "jupyter.log", "ab")  # noqa: SIM115 — живёт вместе с процессом
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "jupyter_server", f"--config={config}"],
                cwd=str(self.root_dir), env=self.env(), stdout=log_file, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            self.started_at = time.monotonic()
            log.info("Jupyter запущен: порт %s, корень %s", self.port, self.root_dir)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self) -> None:
        """Упавший Jupyter поднимаем заново (тот же порт и токен — открытые вкладки переподключатся)."""
        if not self.alive():
            if self.proc is not None:
                self.restarts += 1
                log.warning("Jupyter завершился с кодом %s — перезапуск", self.proc.returncode)
            self.start()

    def stop(self) -> None:
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def ready(self, timeout: float = 30) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.ensure()
            try:
                if httpx.get(self.url("api/status"), headers=self.headers, timeout=3).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False

    def log_tail(self, lines: int = 20) -> str:
        try:
            text = (self.data_dir / "jupyter.log").read_bytes()[-20000:].decode("utf-8", "replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    # --- настройки страниц ----------------------------------------------------------------------------------

    def set_theme(self, theme: str) -> None:
        """Тема JupyterLab и Notebook — как у Пульса (общие настройки обоих интерфейсов)."""
        name = THEMES.get(theme)
        if not name:
            return
        raw = json.dumps({"theme": name, "adaptive-theme": False})
        httpx.put(self.url("lab/api/settings/@jupyterlab/apputils-extension:themes"), headers=self.headers,
                  json={"raw": raw}, timeout=5)
