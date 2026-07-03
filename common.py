"""
Общие утилиты стратег-бота: загрузка .env, форс IPv4, SSL-контекст, логи.
Только стандартная библиотека (без pip) — чтобы работало на голом VPS.

Битрикс-часть (bx_call/bx_list) добавится на этапе B2; здесь заложен базовый
слой, общий для всех этапов.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


# --------------------------------------------------------------------------- #
#  Форс IPv4 — грабли из ПМ-бота (VPS без публичного IPv6 падает на IPv6)
# --------------------------------------------------------------------------- #
def _force_ipv4_dns() -> None:
    """Некоторые VPS без публичного IPv6 всё равно пытаются ходить по IPv6 и
    падают с '[Errno 101] Network is unreachable'. Заставляем DNS отдавать
    только IPv4 — все наши хосты (Telegram, Anthropic, Битрикс) доступны по IPv4."""
    if getattr(socket.getaddrinfo, "_ipv4_only", False):
        return
    _orig = socket.getaddrinfo

    def _gai(host, port, family=0, type=0, proto=0, flags=0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)

    _gai._ipv4_only = True
    socket.getaddrinfo = _gai


_force_ipv4_dns()

PROJECT_DIR = Path(__file__).resolve().parent
CORP_CA = PROJECT_DIR / "corp_ca.pem"   # корень HTTPS-перехватчика (антивирус), если есть — только для локальной разработки
LOGS_DIR = PROJECT_DIR / "logs"
DIALOGS_DIR = LOGS_DIR / "dialogs"      # память диалогов по пользователям
CLAUDE_MD = PROJECT_DIR / "CLAUDE.md"
COMPANY_MD = PROJECT_DIR / "company.md"
DECISIONS_MD = LOGS_DIR / "decisions.md"


class AppError(RuntimeError):
    """Ошибка приложения (сеть / API / конфиг)."""


# --------------------------------------------------------------------------- #
#  .env
# --------------------------------------------------------------------------- #
def load_env(env_path: Path | None = None) -> dict:
    """Читает .env (KEY=VALUE построчно) и кладёт в os.environ, если ещё не задано."""
    env_path = env_path or (PROJECT_DIR / ".env")
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        values[key] = val
        os.environ.setdefault(key, val)
    return values


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise AppError(
            f"Не задана переменная окружения {name}. "
            f"Заполни её в файле .env (шаблон — .env.example)."
        )
    return val


# --------------------------------------------------------------------------- #
#  SSL
# --------------------------------------------------------------------------- #
_SSL_CTX: ssl.SSLContext | None = None


def make_ssl_context() -> ssl.SSLContext:
    """Системные корневые + (если есть) локальный corp_ca.pem от HTTPS-перехватчика.
    Проверка сертификата остаётся ВКЛЮЧЕННОЙ. В облаке файла нет — только системные корни."""
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        if CORP_CA.exists():
            try:
                ctx.load_verify_locations(cafile=str(CORP_CA))
            except ssl.SSLError as exc:
                eprint(f"  ~ не удалось загрузить corp_ca.pem: {exc}")
        _SSL_CTX = ctx
    return _SSL_CTX


def parse_id_list(raw: str) -> set[int]:
    """'419922364,590487361' -> {419922364, 590487361}. Разделители — запятая/точка с запятой."""
    return {int(p) for p in raw.replace(";", ",").split(",")
            if p.strip().lstrip("-").isdigit()}


# --------------------------------------------------------------------------- #
#  Битрикс24 REST (входящий вебхук) — этап B2
# --------------------------------------------------------------------------- #
class BitrixError(AppError):
    """Ошибка на стороне Битрикса или сети."""


BX_TIMEOUT = 30
BX_RETRIES = 3


def _bx_http_post(url: str, payload: dict) -> dict:
    """POST application/json с ретраями на сетевые сбои. Возвращает распарсенный JSON."""
    body = json.dumps(payload).encode("utf-8")
    last: Exception | None = None
    for attempt in range(1, BX_RETRIES + 1):
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=BX_TIMEOUT, context=make_ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last = BitrixError(f"HTTP {exc.code} от Битрикса: {detail[:400]}")
            if 400 <= exc.code < 500 and exc.code != 429:  # логические ошибки не повторяем
                raise last
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionError) as exc:
            last = BitrixError(f"Сеть до Битрикса недоступна: {exc}")
        if attempt < BX_RETRIES:
            time.sleep(1.5 * attempt)
    raise last or BitrixError("Неизвестная ошибка HTTP к Битриксу")


def bx_call(method: str, params: dict | None = None) -> dict:
    """Один вызов метода REST. Возвращает весь ответ (с полями result/next/total)."""
    webhook = require_env("BITRIX_WEBHOOK_URL").rstrip("/")
    data = _bx_http_post(f"{webhook}/{method}.json", params or {})
    if isinstance(data, dict) and data.get("error"):
        raise BitrixError(
            f"Битрикс вернул ошибку по методу {method}: "
            f"{data.get('error')} — {data.get('error_description', '')}"
        )
    return data


def bx_list(method: str, params: dict | None = None, *, max_pages: int = 500) -> list:
    """Постраничная выгрузка списковых методов (crm.deal.list, crm.item.list, ...)."""
    params = dict(params or {})
    items: list = []
    start = 0
    for _ in range(max_pages):
        params["start"] = start
        data = bx_call(method, params)
        result = data.get("result", [])
        if isinstance(result, dict) and "items" in result:  # crm.item.list оборачивает в {"items": [...]}
            result = result["items"]
        if not isinstance(result, list):
            break
        items.extend(result)
        nxt = data.get("next")
        if nxt in (None, "", False):
            break
        start = nxt
    return items


def eprint(*args) -> None:
    """Печать в stderr (не мешает stdout)."""
    print(*args, file=sys.stderr, flush=True)
