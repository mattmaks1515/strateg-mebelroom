"""
Слой Telegram Bot API: вызовы методов, отправка (с разбивкой длинных ответов),
определение «обращаются ли к боту». Взято из ПМ-бота и адаптировано под стратега,
чей ответ бывает длинным (глубокий формат) — поэтому шлём несколькими сообщениями.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

from common import make_ssl_context, require_env, eprint

TG_LIMIT = 4096   # лимит длины одного сообщения Telegram
TG_RETRIES = 3    # повторы при разовых обрывах TLS/сети (антивирус-перехватчик, флаки-сеть)


def _redact(text: str) -> str:
    """Убирает токен бота из строк (логи/ошибки), чтобы он не попал в journald/консоль."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    return text.replace(tok, "<TOKEN>") if tok else text


_HEAD_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*")   # ## Заголовок
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")        # [текст](url)


def to_plain(text: str) -> str:
    """Убирает markdown-разметку, которую Telegram не рендерит (шлём без parse_mode).
    Главное — двойные звёздочки **жирного**, а также #-заголовки, __, `код`, ссылки."""
    if not text:
        return text
    text = _HEAD_RE.sub("", text)                 # снять #-заголовки
    text = _LINK_RE.sub(r"\1 (\2)", text)         # [текст](url) -> текст (url)
    text = text.replace("**", "").replace("__", "")
    text = text.replace("`", "")
    return text


def tg_base() -> str:
    """По умолчанию api.telegram.org; можно переопределить TELEGRAM_API_BASE (прокси)."""
    return os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


def tg_call(method: str, params: dict | None = None, *, timeout: int = 30) -> dict:
    """Вызов метода Telegram Bot API с повторами на разовые сетевые/TLS-обрывы.
    HTTPError (4xx/5xx — логические ошибки) не повторяем, пробрасываем сразу."""
    token = require_env("TELEGRAM_BOT_TOKEN")
    url = f"{tg_base()}/bot{token}/{method}"
    body = json.dumps(params or {}).encode("utf-8")
    last: Exception | None = None
    for attempt in range(1, TG_RETRIES + 1):
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=make_ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionError) as exc:
            last = exc
            if attempt < TG_RETRIES:
                time.sleep(1.5 * attempt)
    raise last or RuntimeError("tg_call: неизвестная ошибка сети")


def _split_for_telegram(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режем длинный ответ на части <= limit, по возможности по границам абзацев/строк."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def send(chat_id, text: str, reply_to=None, thread_id=None) -> None:
    """Отправка ответа. Markdown чистится, длинный текст уходит несколькими сообщениями."""
    chunks = _split_for_telegram(to_plain(text)) or ["Готово."]
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if reply_to and i == 0:  # реплай вешаем только на первое сообщение
            payload["reply_to_message_id"] = reply_to
        if thread_id:
            payload["message_thread_id"] = thread_id
        try:
            tg_call("sendMessage", payload)
        except urllib.error.URLError as exc:
            eprint(f"send fail: {_redact(str(exc))}")


def send_typing(chat_id, thread_id=None) -> None:
    """Показать «печатает…», пока модель думает (ответ бывает не быстрым)."""
    payload = {"chat_id": chat_id, "action": "typing"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        tg_call("sendChatAction", payload)
    except Exception:
        pass


def get_me() -> tuple[str, int]:
    me = tg_call("getMe").get("result", {})
    return me.get("username", ""), me.get("id")


def addressed_to_bot(msg: dict, bot_username: str, bot_id: int, chat_type: str = "") -> tuple[bool, str]:
    """Обращение к боту? В личке — любое сообщение; в группе — reply/@упоминание/«/»."""
    text = (msg.get("text") or "").strip()
    if not text:
        return False, ""
    if chat_type == "private":
        return True, text
    reply = msg.get("reply_to_message") or {}
    if (reply.get("from") or {}).get("id") == bot_id:
        return True, text
    at = f"@{bot_username}"
    if at.lower() in text.lower():
        return True, text.replace(at, "").replace(at.lower(), "").strip()
    if text.startswith("/"):
        return True, text
    return False, ""
