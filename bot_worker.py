"""
Постоянный воркер стратег-бота для VPS: работает 24/7, отвечает через long-polling.
Запуск как сервис systemd (см. deploy/). Секреты — в .env рядом со скриптом.

Отвечает ТОЛЬКО пользователям из белого списка OWNER_USER_ID.
Держит контекст разговора на каждого пользователя (memory.py).

Устойчивость: любая ошибка в цикле логируется и НЕ роняет процесс
(systemd всё равно перезапустит при падении).

Команды:
  /start, /help — краткая справка
  /reset        — забыть историю разговора (начать с чистого листа)
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time

from common import AppError, LOGS_DIR, eprint, load_env, parse_id_list, require_env
from telegram_io import tg_call, get_me, send, send_typing, addressed_to_bot
import brain
from advisor import ask
import bitrix_stats
import digest
import memory

POLL_TIMEOUT = 25   # long-poll: сервер держит соединение до 25с
BITRIX_TTL = 600    # кэш сводки Битрикса между вопросами, сек (запросы к Битриксу не быстрые)

_bx_cache = {"text": "", "ts": 0.0}

# --- B4: еженедельный дайджест ---
DIGEST_STATE = LOGS_DIR / "digest_state.json"


def _digest_enabled() -> bool:
    return os.environ.get("SEND_DIGEST", "1").strip().lower() not in ("0", "false", "no", "off")


def _digest_weekday() -> int:
    try:
        return int(os.environ.get("DIGEST_WEEKDAY", "0"))  # 0 = понедельник
    except ValueError:
        return 0


def _digest_hour() -> int:
    try:
        return int(os.environ.get("DIGEST_HOUR", "9"))
    except ValueError:
        return 9


def _week_key(now: datetime.datetime) -> str:
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _load_digest_state() -> dict:
    if DIGEST_STATE.exists():
        try:
            return json.loads(DIGEST_STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_digest_state(state: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DIGEST_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def maybe_weekly_digest(allowed_ids: set) -> None:
    """Раз в неделю (в назначенный день/час) собирает дайджест и рассылает владельцам.
    Дедуп по номеру ISO-недели, чтобы не слать повторно при рестартах/каждой итерации цикла."""
    if not _digest_enabled():
        return
    now = datetime.datetime.now().astimezone()
    if now.weekday() != _digest_weekday() or now.hour < _digest_hour():
        return
    key = _week_key(now)
    state = _load_digest_state()
    if state.get("last") == key:
        return
    try:
        text = "📊 Еженедельный дайджест рынка (мебель, Уфа)\n\n" + digest.build_digest()
        for uid in allowed_ids:
            send(uid, text)
        state["last"] = key
        _save_digest_state(state)
        eprint(f"[worker] дайджест разослан ({now:%Y-%m-%d %H:%M}, неделя {key})")
    except Exception as exc:
        eprint(f"[worker] дайджест не отправлен, повтор позже: {exc}")


def bitrix_context() -> str:
    """Свежая сводка Битрикса с кэшем. При недоступности Битрикса не роняет ответ —
    возвращает пустую строку (советник ответит из company.md)."""
    now = time.time()
    if _bx_cache["text"] and now - _bx_cache["ts"] < BITRIX_TTL:
        return _bx_cache["text"]
    try:
        _bx_cache["text"] = bitrix_stats.render_summary()
        _bx_cache["ts"] = now
    except Exception as exc:
        eprint(f"[worker] Битрикс недоступен, отвечаю без свежих цифр: {exc}")
        return ""
    return _bx_cache["text"]

HELP_TEXT = (
    "Я — стратег-советник владельца Мебельрум. Пиши вопрос по бизнесу: стратегия, "
    "конкуренты, найм, цены, расширение, экономика заказа — разберу глубоко и с "
    "trade-offs.\n\n"
    "Команды:\n"
    "/правка <текст> — задать мне постоянную правку поведения. Например:\n"
    "   /правка отвечай короче, без длинных вступлений\n"
    "   /правка всегда предлагай конкретный следующий шаг\n"
    "Правка запоминается навсегда и действует сразу.\n"
    "/правки — показать все мои текущие правки\n"
    "/сброс_правок — убрать все правки\n"
    "/reset — забыть наш прошлый разговор\n"
    "/help — эта справка"
)


def _handle_command(cmd: str, chat_id, user_id: int, reply_to, thread_id) -> bool:
    """Обрабатывает служебные команды. True — если это была команда (ответ отправлен)."""
    parts = cmd.strip().split(maxsplit=1)
    name = parts[0].lower().lstrip("/").split("@")[0] if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    if name in ("start", "help"):
        send(chat_id, HELP_TEXT, reply_to, thread_id)
        return True
    if name == "reset":
        memory.reset(user_id)
        send(chat_id, "Готово — забыл наш прошлый разговор. Начнём с чистого листа.", reply_to, thread_id)
        return True
    if name in ("правка", "запомни"):
        if not arg:
            send(chat_id, "Напиши правку после команды. Например:\n/правка отвечай короче",
                 reply_to, thread_id)
            return True
        brain.add_override(arg)
        send(chat_id, f"Готово, запомнил как постоянную правку и применю сразу:\n{arg}",
             reply_to, thread_id)
        return True
    if name in ("правки", "мои_правки"):
        ov = brain.load_overrides()
        send(chat_id, ("Мои текущие правки:\n\n" + ov) if ov else "Пока правок нет.",
             reply_to, thread_id)
        return True
    if name in ("сброс_правок", "сбросить_правки", "очистить_правки"):
        brain.clear_overrides()
        send(chat_id, "Убрал все правки. Работаю по базовым инструкциям.", reply_to, thread_id)
        return True
    return False


NOTICE_AFTER = 300  # сек: предупреждаем, только если ответ идёт дольше 5 минут (раньше — молчим)
LONG_NOTICE = (
    "Секунду — вопрос ёмкий, собираю данные и думаю. "
    "Для разбора конкурента с веб-поиском нужно несколько минут. Пришлю, как будет готово."
)


def _typing_loop(chat_id, thread_id, stop: threading.Event) -> None:
    """Держит статус «печатает…» живым и один раз предупреждает, если ответ затянулся."""
    waited, notified = 0.0, False
    while not stop.wait(4):
        waited += 4
        send_typing(chat_id, thread_id)
        if not notified and waited >= NOTICE_AFTER:
            send(chat_id, LONG_NOTICE, None, thread_id)
            notified = True


def handle_message(msg: dict, bot_username: str, bot_id: int, allowed_ids: set) -> None:
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    user_id = frm.get("id")
    thread_id = msg.get("message_thread_id")
    reply_to = msg.get("message_id")

    if user_id not in allowed_ids:
        return
    ok, clean = addressed_to_bot(msg, bot_username, bot_id, chat.get("type", ""))
    if not ok:
        return

    # служебные команды
    if clean.startswith("/"):
        if _handle_command(clean, chat.get("id"), user_id, reply_to, thread_id):
            return
        clean = clean.lstrip("/").strip()
        if not clean:
            return

    # мозг собираем свежим на каждый вопрос: правки (/правка) и company.md применяются сразу
    system_prompt = brain.build_system_prompt()

    stop = threading.Event()
    typer = threading.Thread(target=_typing_loop, args=(chat.get("id"), thread_id, stop), daemon=True)
    send_typing(chat.get("id"), thread_id)
    typer.start()
    try:
        history = memory.load_history(user_id)
        reply = ask(clean, system_prompt, history, live_data=bitrix_context())
    except Exception as exc:
        stop.set()
        send(chat.get("id"), f"Не смог обработать запрос: {exc}", reply_to, thread_id)
        return
    finally:
        stop.set()

    send(chat.get("id"), reply, reply_to, thread_id)
    memory.save_turn(user_id, clean, reply)


def main() -> int:
    load_env()
    allowed_ids = parse_id_list(require_env("OWNER_USER_ID"))
    if not allowed_ids:
        eprint("OWNER_USER_ID пуст или не содержит числовых id.")
        return 1

    # проверим ключ Anthropic заранее — понятная ошибка при старте, а не в первом ответе
    require_env("ANTHROPIC_API_KEY")

    system_prompt = brain.build_system_prompt()
    bot_username, bot_id = get_me()
    eprint(f"[worker] запущен как @{bot_username}, пользователей в whitelist: {len(allowed_ids)}, "
           f"system-prompt: {len(system_prompt)} символов")

    eprint(f"[worker] дайджест: {'вкл' if _digest_enabled() else 'выкл'} "
           f"(день недели {_digest_weekday()}, час {_digest_hour()})")

    offset = None
    while True:
        try:
            maybe_weekly_digest(allowed_ids)
            params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            data = tg_call("getUpdates", params, timeout=POLL_TIMEOUT + 20)
            for u in data.get("result", []):
                offset = u.get("update_id", 0) + 1
                m = u.get("message")
                if m:
                    handle_message(m, bot_username, bot_id, allowed_ids)
        except AppError as exc:
            eprint(f"[worker] ошибка конфигурации: {exc}")
            time.sleep(5)
        except Exception as exc:
            eprint(f"[worker] ошибка цикла (продолжаю): {exc}")
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
