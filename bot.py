import asyncio
import contextlib
import html
import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

from telethon import TelegramClient, events
from telethon.errors import AuthKeyError, FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl import functions, types

import proxies
import userbot

logger = logging.getLogger("danybot.bot")

STATE_FILE = Path(__file__).parent / "state_bot.json"
HISTORY_FILE = Path(__file__).parent / "history_bot.json"

model_overrides: dict[int, str] = {}
auto_respond: set[int] = set()
ignored_chats: set[int] = set()
ignored_users: set[int] = set()

chat_history: dict[int, deque] = {}
ctx_lock = asyncio.Lock()
recent_reply_ids: set[int] = set()
last_chat_activity: dict[int, float] = {}

bot_username = ""
bot_id = 0


def _mention_re():
    if not bot_username:
        return None
    return re.compile(r"@" + re.escape(bot_username) + r"\b", re.IGNORECASE)


def _is_mentioned(text):
    rx = _mention_re()
    return bool(rx and rx.search(text))


def _strip_mention(text):
    rx = _mention_re()
    if rx:
        return rx.sub("", text, count=1).strip()
    return text


BOT_COMMANDS = {
    "start": ("start", "help", "помощь", "хелп", "справка", "начать", "старт", "?"),
    "clear": (
        "clear",
        "сброс",
        "сбросить",
        "забыть",
        "забудь",
        "стоп",
        "очистить",
        "очистка",
    ),
    "model": ("model", "модель"),
    "models": ("models", "модели"),
    "history": ("history", "история", "контекст", "ctx"),
    "ping": ("ping", "пинг", "check", "чек"),
    "auto": ("auto", "авто", "danyauto", "автоответ"),
}

_BOT_CMD_LOOKUP = {
    alias: name for name, aliases in BOT_COMMANDS.items() for alias in aliases
}

BOT_HELP_TEXT = (
    "DanyBOT — команды / commands:\n"
    "/help /start — справка / help\n"
    "/model <id> — сменить модель / set model\n"
    "/models — список моделей / list models\n"
    "/clear — очистить контекст / clear context\n"
    "/history — размер контекста / context size\n"
    "/ping — статус / status\n"
    "/auto on|off — авто-ответ / auto-reply\n\n"
    "Также работает / Also works: @упоминание, реплай боту, .db-триггеры."
)


def handle_bot_commands(text) -> tuple[str, Any] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]
    if not body:
        return None
    parts = body.split(maxsplit=1)
    head = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    if "@" in head:
        head = head.split("@", 1)[0]
    cmd = _BOT_CMD_LOOKUP.get(head.lower())
    if cmd is None:
        return None
    if cmd == "auto":
        low = arg.lower()
        if low in userbot.AUTO_ON_WORDS:
            return ("autorespond", True)
        if low in userbot.AUTO_OFF_WORDS:
            return ("autorespond", False)
        return ("auto_status", None)
    if cmd == "model":
        return ("model", arg or None)
    if cmd == "models":
        return ("models", None)
    if cmd == "start":
        return ("help", None)
    return (cmd, None)


bot_client = TelegramClient(
    StringSession(),
    userbot.API_ID,
    userbot.API_HASH,
    connection_retries=2,
    request_retries=1,
    retry_delay=0,
    timeout=10,
)


def load_state():
    global model_overrides, auto_respond, ignored_chats, ignored_users
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        model_overrides = {
            int(k): v for k, v in data.get("model_overrides", {}).items()
        }
        auto_respond = {int(x) for x in data.get("auto_respond", [])}
        ignored_chats = {int(x) for x in data.get("ignored_chats", [])}
        ignored_users = {int(x) for x in data.get("ignored_users", [])}
    except (
        json.JSONDecodeError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        logger.warning("Не удалось загрузить state_bot.json: %s", exc)


def save_state():
    data = {
        "model_overrides": {str(k): v for k, v in model_overrides.items()},
        "auto_respond": sorted(auto_respond),
        "ignored_chats": sorted(ignored_chats),
        "ignored_users": sorted(ignored_users),
    }
    try:
        STATE_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except (OSError, TypeError) as exc:
        logger.warning("Не удалось сохранить state_bot.json: %s", exc)


def load_history():
    if not HISTORY_FILE.exists():
        return
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        for key, value in data.items():
            try:
                chat_id = int(key)
            except (ValueError, TypeError):
                continue
            limit = (
                userbot.DM_HISTORY_LIMIT if chat_id > 0 else userbot.GROUP_HISTORY_LIMIT
            )
            chat_history[chat_id] = deque(value, maxlen=limit)
    except (
        json.JSONDecodeError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        logger.warning("Не удалось загрузить history_bot.json: %s", exc)


def save_history():
    data = {str(k): list(v) for k, v in chat_history.items()}
    try:
        HISTORY_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except (OSError, TypeError) as exc:
        logger.warning("Не удалось сохранить history_bot.json: %s", exc)


def models_text(chat_id) -> str:
    current = model_overrides.get(chat_id, userbot.DANYAPI_MODEL)
    parts = ["Доступные модели / Available models:"]
    for m in userbot.MODELS:
        marker = " (текущая) / current" if m == current else ""
        parts.append(f"• {m}{marker}")
    if current not in userbot.MODELS:
        parts.append(f"• {current} (текущая) / current")
    return "\n".join(parts)


async def safe_reply(event, text):
    for _attempt in range(userbot.REPLY_ATTEMPTS):
        try:
            sent = await event.reply(text)
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 30))
            continue
        except (RPCError, OSError, ValueError, TypeError):
            return None
        if sent:
            recent_reply_ids.add(sent.id)
            if len(recent_reply_ids) > 5000:
                recent_reply_ids.clear()
        return sent
    return None


async def edit_text(chat_id, msg_id, text):
    for _attempt in range(userbot.REPLY_ATTEMPTS):
        try:
            await bot_client.edit_message(chat_id, msg_id, text, parse_mode="html")
            return True
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 30))
            continue
        except (RPCError, OSError, ValueError, TypeError):
            return False
    return False


@bot_client.on(events.NewMessage(incoming=None))
async def handler(event: events.NewMessage.Event):
    message = event.message
    if not message or not message.message:
        return

    text = message.message
    msg_id = message.id
    chat_id = event.chat_id
    if chat_id is None:
        return
    sender_id = event.sender_id
    is_self = bool(message.out)

    if msg_id in recent_reply_ids:
        return

    is_private = event.is_private
    triggered = bool(userbot.TRIGGER_RE.search(text))
    mentioned = _is_mentioned(text)
    now = time.monotonic()

    reply_to_bot = False
    if message.is_reply and not is_self:
        try:
            reply_msg = await message.get_reply_message()
            if reply_msg is not None and (
                getattr(reply_msg, "out", False)
                or (bot_id and getattr(reply_msg, "sender_id", None) == bot_id)
            ):
                reply_to_bot = True
        except (RPCError, OSError, ValueError):
            reply_to_bot = False

    try:
        command = handle_bot_commands(text)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        command = None
    if command is None:
        try:
            command = userbot.handle_commands(text)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            command = None

    if command and command[0] in ("ignore", "unignore"):
        return

    if command:
        cmd = command[0]
        if cmd == "clear":
            async with ctx_lock:
                limit = (
                    userbot.DM_HISTORY_LIMIT
                    if is_private
                    else userbot.GROUP_HISTORY_LIMIT
                )
                chat_history[chat_id] = deque(maxlen=limit)
            save_history()
            await safe_reply(event, "Контекст очищен. / Context cleared.")
            return
        if cmd == "model":
            val = command[1]
            async with ctx_lock:
                if val:
                    model_overrides[chat_id] = val
                    text_out = f"Модель установлена / Model set: {val}"
                else:
                    current = model_overrides.get(chat_id, userbot.DANYAPI_MODEL)
                    text_out = f"Текущая модель / Current model: {current}"
            save_state()
            await safe_reply(event, text_out)
            return
        if cmd == "autorespond":
            val = command[1]
            async with ctx_lock:
                if val:
                    auto_respond.add(chat_id)
                    text_out = "Авто-ответ ВКЛ. / Auto-reply ON."
                else:
                    auto_respond.discard(chat_id)
                    text_out = "Авто-ответ ВЫКЛ. / Auto-reply OFF."
            save_state()
            await safe_reply(event, text_out)
            return
        if cmd == "history":
            async with ctx_lock:
                hist = chat_history.get(chat_id, deque())
                n = len(hist)
                chars = sum(len(m["content"]) for m in hist)
            await safe_reply(
                event,
                f"Сообщений в контексте / Messages in context: {n}, "
                f"символов / chars: {chars}",
            )
            return
        if cmd == "help":
            await safe_reply(event, BOT_HELP_TEXT)
            return
        if cmd == "auto_status":
            enabled = chat_id in auto_respond or userbot.AUTO_RESPOND_GLOBAL
            state = "ON" if enabled else "OFF"
            await safe_reply(event, f"Авто-ответ / Auto-reply: {state}")
            return
        if cmd == "models":
            await safe_reply(event, models_text(chat_id))
            return
        if cmd == "ping":
            current = model_overrides.get(chat_id, userbot.DANYAPI_MODEL)
            ctx_len = len(chat_history.get(chat_id, deque()))
            await safe_reply(
                event,
                f"Онлайн / Online. Модель / Model: {current}\n"
                f"Контекст / Context: {ctx_len} сообщений / messages",
            )
            return

    if is_private and text.strip() != "…":
        clean_text = (
            userbot.TRIGGER_RE.sub("", text, count=1).strip()
            if triggered
            else text.strip()
        )
        if is_self:
            role = "user" if triggered else "assistant"
            content = userbot.strip_role_tag(clean_text or text.strip())
        else:
            role = "user"
            label = await userbot.get_sender_label(event)
            content = f"{label}: {clean_text or text.strip()}"
        async with ctx_lock:
            hist = chat_history.setdefault(
                chat_id, deque(maxlen=userbot.DM_HISTORY_LIMIT)
            )
            hist.append({"role": role, "content": content})
            save_history()

    if triggered or mentioned or reply_to_bot or (is_private and not is_self):
        effective_trigger = True
    elif not is_self:
        effective_trigger = chat_id in auto_respond or userbot.AUTO_RESPOND_GLOBAL
    else:
        effective_trigger = False

    if not effective_trigger:
        return

    if userbot.COOLDOWN > 0 and not is_self:
        last = last_chat_activity.get(chat_id, 0)
        if (now - last) < userbot.COOLDOWN:
            logger.info("Кулдаун для чата %s, пропускаю", chat_id)
            return

    last_chat_activity[chat_id] = time.monotonic()
    if len(last_chat_activity) > 10000:
        cutoff = time.monotonic() - 3600
        for k in list(last_chat_activity):
            if last_chat_activity[k] < cutoff:
                del last_chat_activity[k]

    logger.info("Бот: запрос из чата %s от %s: %s", chat_id, sender_id, text[:100])

    prompt = _strip_mention(userbot.TRIGGER_RE.sub("", text, count=1).strip())

    replied_text = None
    try:
        if message.is_reply:
            reply_msg = await message.get_reply_message()
            if reply_msg and reply_msg.message:
                replied_text = reply_msg.message.strip()
    except (RPCError, OSError, ValueError):
        replied_text = None

    if replied_text:
        if prompt:
            prompt = (
                f"Сообщение, на которое ответили:\n{replied_text}\n\nЗапрос: {prompt}"
            )
        else:
            prompt = replied_text

    if not prompt:
        return

    if len(prompt) > userbot.MAX_REQUEST_LEN:
        prompt = prompt[: userbot.MAX_REQUEST_LEN]

    if is_private:
        async with ctx_lock:
            hist = chat_history.setdefault(
                chat_id, deque(maxlen=userbot.DM_HISTORY_LIMIT)
            )
            sysp = userbot.system_for(chat_id, mode="bot")
            model = model_overrides.get(chat_id, userbot.DANYAPI_MODEL)
            messages = [{"role": "system", "content": sysp}, *list(hist)]
    else:
        label = await userbot.get_sender_label(event)
        user_content = f"{label}: {prompt}" if label else prompt
        async with ctx_lock:
            hist = chat_history.setdefault(
                chat_id, deque(maxlen=userbot.GROUP_HISTORY_LIMIT)
            )
            hist.append({"role": "user", "content": user_content})
            sysp = userbot.system_for(chat_id, mode="bot")
            model = model_overrides.get(chat_id, userbot.DANYAPI_MODEL)
            messages = [{"role": "system", "content": sysp}, *list(hist)]

    if is_self:
        prefix = f"{html.escape(text)}\n\n{model}:\n\n"
        edit_id = msg_id
    else:
        prefix = f"{model}:\n\n"
        edit_id = None

    try:
        full_answer = ""
        last_edit = 0.0
        reasoning_parts: list[str] = []
        tool_parts: list[str] = []

        def render():
            return userbot.render_response(
                prefix, reasoning_parts, tool_parts, full_answer
            )

        async def on_delta(part):
            nonlocal full_answer, last_edit
            full_answer += part
            now_m = time.monotonic()
            if edit_id is not None and (now_m - last_edit) >= userbot.EDIT_INTERVAL:
                await edit_text(chat_id, edit_id, render())
                last_edit = now_m

        async def on_reasoning(part):
            nonlocal last_edit
            reasoning_parts.append(part)
            now_m = time.monotonic()
            if edit_id is not None and (now_m - last_edit) >= userbot.EDIT_INTERVAL:
                await edit_text(chat_id, edit_id, render())
                last_edit = now_m

        async def on_tool(name):
            nonlocal last_edit
            tool_parts.append(name)
            now_m = time.monotonic()
            if edit_id is not None and (now_m - last_edit) >= userbot.EDIT_INTERVAL:
                await edit_text(chat_id, edit_id, render())
                last_edit = now_m

        async with cast(Any, bot_client.action(chat_id, "typing")):
            if not is_self:
                placeholder = await event.reply("…")
                if placeholder:
                    edit_id = placeholder.id
                    recent_reply_ids.add(edit_id)

            result = await userbot.stream_with_tools(
                messages,
                model,
                chat_id,
                on_delta,
                on_reasoning,
                on_tool,
                client_override=bot_client,
                tools=userbot.TOOLS,
            )

        if result:
            full_answer = result
        if edit_id is not None:
            await edit_text(chat_id, edit_id, render())

        async with ctx_lock:
            hist.append({"role": "assistant", "content": full_answer})
            save_history()

        if len(recent_reply_ids) > 5000:
            recent_reply_ids.clear()

    except (userbot.OpenAIError, RPCError, OSError, ValueError, TypeError):
        logger.exception("Бот: ошибка генерации ответа")
        await safe_reply(
            event, "Ошибка при обращении к DanyAPI. / DanyAPI request error."
        )


async def start_bot():
    global bot_username, bot_id
    if not userbot.BOT_TOKEN:
        logger.error("ENABLE_BOT=1, но BOT_TOKEN не задан")
        return

    load_state()
    load_history()

    candidates = await proxies.get_proxy_candidates(limit=40)
    if not candidates:
        logger.warning("Бот: нет прокси, пробую напрямую")
        candidates = [None]

    for idx, proxy in enumerate(candidates):
        if proxy:
            logger.info("Бот: пробую прокси %d/%d: %s", idx + 1, len(candidates), proxy)
            bot_client.set_proxy(proxy)
        try:
            start_coro = cast(Any, bot_client.start(bot_token=userbot.BOT_TOKEN))
            await asyncio.wait_for(start_coro, timeout=25)
            me = await bot_client.get_me()
            bot_username = getattr(me, "username", "") or ""
            bot_id = getattr(me, "id", 0) or 0
            logger.info("Бот запущен как @%s", bot_username or "?")
            try:
                await bot_client(
                    functions.bots.SetBotCommandsRequest(
                        scope=types.BotCommandScopeDefault(),
                        lang_code="",
                        commands=[
                            types.BotCommand(
                                command="help", description="Справка / Help"
                            ),
                            types.BotCommand(
                                command="model",
                                description="Сменить модель / Set model",
                            ),
                            types.BotCommand(
                                command="models",
                                description="Список моделей / List models",
                            ),
                            types.BotCommand(
                                command="clear",
                                description="Очистить контекст / Clear context",
                            ),
                            types.BotCommand(
                                command="history",
                                description="Размер контекста / Context size",
                            ),
                            types.BotCommand(
                                command="ping", description="Статус / Status"
                            ),
                            types.BotCommand(
                                command="auto",
                                description="Авто-ответ on/off / Auto-reply on/off",
                            ),
                        ],
                    )
                )
            except (RPCError, OSError, ValueError, TypeError) as exc:
                logger.warning("Не удалось задать меню команд: %s", exc)
            break
        except AuthKeyError as exc:
            logger.error("Токен бота невалиден: %s", exc)
            return
        except (
            RPCError,
            ConnectionError,
            OSError,
            TimeoutError,
            EOFError,
            BufferError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning(
                "Бот: не удалось подключиться через %s: %s",
                proxy,
                type(exc).__name__,
            )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(cast(Any, bot_client.disconnect()), timeout=10)
            if proxy:
                proxies.mark_bad_proxy(proxies.telethon_to_item(proxy))
    else:
        logger.error("Бот: не удалось подключиться ни через один прокси")
        return

    await cast(Any, bot_client.run_until_disconnected())


async def disconnect_quietly(timeout=10):
    with contextlib.suppress(Exception):
        await asyncio.wait_for(cast(Any, bot_client.disconnect()), timeout=timeout)
