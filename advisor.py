"""
Вызов Claude Messages API — «думающая» часть стратег-бота.
Модель по умолчанию — Fable 5 (максимум качества рассуждений); задаётся ANTHROPIC_MODEL.

Запрос устойчивый (как в ПМ-боте): без output_config/thinking — на Fable 5 явный
thinking/temperature вернул бы 400. Ответ — свободный развёрнутый текст по формату из CLAUDE.md.

Этап B3 — веб-поиск: серверный инструмент web_search_20260209 (работает на Fable 5),
локация — Уфа, чтобы конкуренты и рынок искались локально. Anthropic сам выполняет поиск
на своей стороне; если серверный цикл прерывается (stop_reason=pause_turn) — досылаем запрос.
Отключается переменной WEB_SEARCH=0.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from common import make_ssl_context

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-fable-5"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 600      # сек. Fable 5 + веб-поиск на сложных вопросах думает минутами
DEFAULT_EFFORT = "medium"  # глубина/скорость: low|medium|high|xhigh|max (medium — баланс для чата)
MAX_CONTINUATIONS = 5      # сколько раз досылаем запрос при pause_turn (серверный цикл поиска)


def _web_search_enabled() -> bool:
    return os.environ.get("WEB_SEARCH", "1").strip().lower() not in ("0", "false", "no", "off")


def _tools() -> list[dict]:
    if not _web_search_enabled():
        return []
    try:
        max_uses = int(os.environ.get("WEB_SEARCH_MAX_USES", "8") or 8)
    except ValueError:
        max_uses = 8
    return [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": max_uses,
        "user_location": {
            "type": "approximate",
            "city": "Ufa",
            "region": "Bashkortostan",
            "country": "RU",
            "timezone": "Asia/Yekaterinburg",
        },
    }]


def _call_api(body: dict, api_key: str, timeout: int) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=make_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic HTTP {exc.code}: {detail[:300]}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Сеть до api.anthropic.com недоступна: {exc}")


def ask(user_text: str, system_prompt: str, history: list[dict],
        *, live_data: str = "", timeout: int | None = None) -> str:
    """Возвращает текст ответа советника. Бросает RuntimeError при ошибке сети/API.

    live_data — свежие цифры из Битрикса на текущий запрос. Кладём их в текст
    вопроса (а не в историю), чтобы память диалога не копила устаревшие данные."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Не задан ANTHROPIC_API_KEY")
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    except ValueError:
        max_tokens = DEFAULT_MAX_TOKENS
    if timeout is None:
        try:
            timeout = int(os.environ.get("ANTHROPIC_TIMEOUT", DEFAULT_TIMEOUT))
        except ValueError:
            timeout = DEFAULT_TIMEOUT
    effort = os.environ.get("ANTHROPIC_EFFORT", DEFAULT_EFFORT).strip()

    content = user_text
    if live_data:
        content = (
            f"[АКТУАЛЬНЫЕ ДАННЫЕ ИЗ БИТРИКСА — используй их для цифр]\n{live_data}\n\n"
            f"Вопрос: {user_text}"
        )
    messages = list(history) + [{"role": "user", "content": content}]
    tools = _tools()

    data = {}
    for _ in range(MAX_CONTINUATIONS + 1):
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
        if effort:
            body["output_config"] = {"effort": effort}  # глубина рассуждений (не thinking — на Fable 5 ок)
        if tools:
            body["tools"] = tools
        data = _call_api(body, api_key, timeout)
        if data.get("stop_reason") == "pause_turn":
            # серверный цикл поиска прервался — досылаем ассистентский ход и продолжаем
            messages = messages + [{"role": "assistant", "content": data.get("content", [])}]
            continue
        break

    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    return text or "Не удалось сформулировать ответ — переформулируй вопрос, пожалуйста."
