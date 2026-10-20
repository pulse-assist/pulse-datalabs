"""Кто может говорить с Jupyter через прокси плагина.

Прокси подставляет токен Jupyter — значит, кто дошёл до прокси, тот выполняет код. Дойти должны только:
- Пульс: /plugin-apps/datalabs/… за сессией владельца. Запрос приходит на 127.0.0.1:<порт>; HTTP из браузера
  владельца несёт Origin адреса Пульса (его же Пульс кладёт в X-Forwarded-Host), WebSocket и команды из ядра
  Пульса — без Origin;
- процессы сервера (агенты) — без Origin.
Отсекаем страницу чужого сайта в любом браузере сервера (Chrome плагина «Браузер» в сети хоста): у неё Origin
чужого сайта (свой заголовок X-Forwarded-Host она без CORS не поставит), а при подмене DNS (rebinding) — чужой Host.
"""

from __future__ import annotations

from urllib.parse import urlsplit

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "app"}


def _hostname(value: str) -> str:
    return (urlsplit("//" + value.strip()).hostname or "").lower()


def allowed(host: str | None, origin: str | None, forwarded_host: str | None = None,
            public_url: str | None = None) -> bool:
    if _hostname(host or "") not in LOCAL_HOSTS:
        return False
    if not origin:
        return True
    if origin == "null":
        return False                  # песочница без same-origin, file://
    parts = urlsplit(origin)
    netloc = parts.netloc.lower()
    if (parts.hostname or "") in LOCAL_HOSTS:
        return True                   # Пульс через SSH-туннель (http://localhost:8001)
    if forwarded_host and netloc == forwarded_host.strip().lower():
        return True
    if public_url:
        public = urlsplit(public_url)
        return (parts.scheme, netloc) == (public.scheme, public.netloc.lower())
    return False
