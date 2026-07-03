"""
Память диалога на каждого пользователя (простое файловое хранилище).
Советнику нужен контекст разговора: держим последние MAX_TURNS обменов
в logs/dialogs/<user_id>.json. Без БД — JSON-файл на пользователя.

Команды управления памятью обрабатываются в bot_worker (/reset).
"""

from __future__ import annotations

import json
import time

from common import DIALOGS_DIR

MAX_TURNS = 12  # сколько последних сообщений (user+assistant) держим в контексте


def _path(user_id: int):
    return DIALOGS_DIR / f"{user_id}.json"


def load_history(user_id: int) -> list[dict]:
    """Список сообщений в формате Messages API: [{'role','content'}, ...]."""
    p = _path(user_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        msgs = data.get("messages", []) if isinstance(data, dict) else []
        return msgs if isinstance(msgs, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_turn(user_id: int, user_text: str, assistant_text: str) -> None:
    """Дописать один обмен и подрезать историю до MAX_TURNS сообщений."""
    DIALOGS_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history(user_id)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    history = history[-MAX_TURNS:]
    payload = {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "messages": history}
    _path(user_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def reset(user_id: int) -> None:
    p = _path(user_id)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
