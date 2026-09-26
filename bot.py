import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

from telethon import Button, TelegramClient, events
from telethon.errors import AuthKeyError, RPCError
from telethon.sessions import StringSession
from telethon.tl import functions, types

import core
import proxies
import tools as tools_module
import userbot

logger = logging.getLogger("danybot.bot")

BOT_COMMANDS = core.BOT_COMMANDS
handle_bot_commands = core.handle_bot_commands

STATE_FILE = Path(__file__).parent / "state_bot.json"
HISTORY_FILE = Path(__file__).parent / "history_bot.json"
CALLBACK_MAX_BYTES = 64

model_overrides: dict[int, str] = {}
coder_chats: set[int] = set()
reasoning_hidden: set[int] = set()
tools_hidden: set[int] = set()

chat_history: dict[int, deque] = {}
ctx_lock = asyncio.Lock()
recent_reply_ids: set[int] = set()
seen_msg_keys: set[tuple[int, int]] = set()
last_chat_activity: dict[int, float] = {}

STORE: core.ModeStore = core.ModeStore(globals())

bot_username = ""
bot_id = 0


_MENTION_RX = None
_MENTION_USER = ""


def _mention_re():
    global _MENTION_RX, _MENTION_USER
    if not bot_username:
        return None
    if _MENTION_USER != bot_username:
        _MENTION_USER = bot_username
        _MENTION_RX = re.compile(r"@" + re.escape(bot_username) + r"\b", re.IGNORECASE)
    return _MENTION_RX


def _is_mentioned(text):
    rx = _mention_re()
    return bool(rx and rx.search(text))


def _strip_mention(text):
    rx = _mention_re()
    if rx:
        return rx.sub("", text, count=1).strip()
    return text


def _db_triggered(text):
    return False


BOT_HELP_TEXT = (
    "DanyBOT - команды / commands:\n"
    "/help /start - справка / help\n"
    "/clear - очистить контекст / clear context\n"
    "/settings - настройки, инлайн-меню / settings, inline menu\n\n"
    "Модель, рассуждения, инструменты, кодер-режим и системный\n"
    "промпт - в меню /settings.\n\n"
    "Также работает / Also works: @упоминание, реплай боту."
)


bot_client = None
START_TIME = time.monotonic()


def _bot_stats(chat_id):
    return {
        "uptime_seconds": round(time.monotonic() - START_TIME, 1),
        "context_messages": len(chat_history.get(chat_id, deque())),
        "model": model_overrides.get(chat_id, userbot.DANYAPI_MODEL),
    }


def get_bot_client():
    global bot_client
    if bot_client is None:
        bot_client = TelegramClient(
            StringSession(),
            userbot.API_ID,
            userbot.API_HASH,
            connection_retries=2,
            request_retries=1,
            retry_delay=0,
            timeout=10,
        )
    return bot_client


def load_state():
    core.load_state_into(STORE, STATE_FILE, logger)


def save_state():
    core.save_state_from(STORE, STATE_FILE, logger)


def load_history():
    core.load_history_into(
        STORE,
        HISTORY_FILE,
        userbot.DM_HISTORY_LIMIT,
        userbot.GROUP_HISTORY_LIMIT,
        logger,
    )


def save_history():
    core.save_history_from(STORE, HISTORY_FILE, logger)


HISTORY_SAVER = core.AsyncSaver(save_history, delay=0.5, logger=logger)


def is_coder(chat_id):
    return chat_id in coder_chats


def coder_active_for(sender_id, chat_id):
    return chat_id in coder_chats and sender_id in userbot.OWNER_IDS


async def _toggle(chat_id, target) -> None:
    async with ctx_lock:
        if chat_id in target:
            target.discard(chat_id)
        else:
            target.add(chat_id)


async def safe_reply(event, text):
    return await core.safe_reply(event, text, userbot.REPLY_ATTEMPTS, recent_reply_ids)


async def edit_text(chat_id, msg_id, text, logger=None):
    return await core.edit_text(
        get_bot_client(), chat_id, msg_id, text, userbot.REPLY_ATTEMPTS, logger
    )


async def handler(event: Any):
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
    triggered = _db_triggered(text)
    mentioned = _is_mentioned(text)
    now = time.monotonic()

    reply_to_bot = False
    replied_text = None
    if message.is_reply and not is_self:
        try:
            reply_msg = await message.get_reply_message()
            if reply_msg is not None:
                if getattr(reply_msg, "out", False) or (
                    bot_id and getattr(reply_msg, "sender_id", None) == bot_id
                ):
                    reply_to_bot = True
                if reply_msg.message:
                    replied_text = reply_msg.message.strip()
        except (RPCError, OSError, ValueError):
            reply_to_bot = False

    try:
        command = handle_bot_commands(text)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        command = None

    if command and command[0] in ("coder", "coder_status"):
        if sender_id not in userbot.OWNER_IDS:
            await safe_reply(event, "Кодер-режим доступен только владельцу.")
            return
        if command[0] == "coder_status":
            state = "ON" if is_coder(chat_id) else "OFF"
            await safe_reply(event, f"Кодер-режим / Coder mode: {state}")
            return
        async with ctx_lock:
            if command[1]:
                coder_chats.add(chat_id)
            else:
                coder_chats.discard(chat_id)
        save_state()
        if command[1]:
            names = ", ".join(t["function"]["name"] for t in tools_module.CODER_TOOLS)
            await safe_reply(
                event,
                "Кодер-режим ВКЛ. Телеграм-функции отключены.\n"
                f"Инструменты: {names}\n"
                f"Корень: {tools_module.CODER_ROOT}\n"
                "Выключить: /coder off",
            )
        else:
            await safe_reply(event, "Кодер-режим ВЫКЛ.")
        return

    if command and command[0] == "prompt":
        if sender_id not in userbot.OWNER_IDS:
            await safe_reply(event, "Системный промпт доступен только владельцу.")
            return
        mode = "coder" if coder_active_for(sender_id, chat_id) else "bot"
        await safe_reply(
            event,
            userbot.system_prompt_report(
                chat_id, mode=mode, with_contract=(mode == "coder")
            ),
        )
        return

    if command and command[0] == "settings":
        if sender_id not in userbot.OWNER_IDS:
            await safe_reply(event, "Настройки доступны только владельцу.")
            return
        try:
            await event.reply(_settings_text(chat_id), buttons=_settings_rows(chat_id))
        except (RPCError, OSError, ValueError, TypeError):
            logger.exception("Бот: не удалось отправить меню настроек")
        return

    if command and command[0] in core.VISIBILITY_COMMANDS:
        if sender_id not in userbot.OWNER_IDS:
            await safe_reply(event, "Переключение доступно только владельцу.")
            return
        text_out, changed = core.apply_visibility_command(
            command, chat_id, reasoning_hidden, tools_hidden
        )
        if changed:
            save_state()
        await safe_reply(event, text_out)
        return

    if command:
        resp = core.handle_command_state(
            command,
            chat_id,
            is_private,
            chat_history,
            model_overrides,
            userbot.DM_HISTORY_LIMIT,
            userbot.GROUP_HISTORY_LIMIT,
            userbot.DANYAPI_MODEL,
            userbot.MODELS,
            BOT_HELP_TEXT,
        )
        if resp is not None:
            text_out, save_s, save_h = resp
            if save_s:
                save_state()
            if save_h:
                save_history()
            await safe_reply(event, text_out)
            return

    await core.append_message_context(
        STORE,
        chat_id,
        msg_id,
        is_private,
        text,
        is_self,
        triggered,
        lambda t, tr: t.strip(),
        userbot.strip_role_tag,
        lambda: userbot.get_sender_label(event),
        userbot.DM_HISTORY_LIMIT,
        userbot.GROUP_HISTORY_LIMIT,
        HISTORY_SAVER.mark_dirty,
    )

    coder_active = coder_active_for(sender_id, chat_id)

    if not (
        triggered
        or coder_active
        or mentioned
        or reply_to_bot
        or (is_private and not is_self)
    ):
        return

    if (
        userbot.COOLDOWN > 0
        and not is_self
        and core.check_cooldown(chat_id, now, userbot.COOLDOWN, last_chat_activity)
    ):
        logger.info("Кулдаун для чата %s, пропускаю", chat_id)
        return
    if is_self:
        core.check_cooldown(chat_id, now, userbot.COOLDOWN, last_chat_activity)

    logger.info("Бот: запрос из чата %s от %s: %s", chat_id, sender_id, text[:100])

    prompt = _strip_mention(text.strip())

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

    model = model_overrides.get(chat_id, userbot.DANYAPI_MODEL)
    mode = "coder" if coder_active else "bot"

    def system_fn(_cid):
        return userbot.system_for(_cid, mode=mode, with_contract=(mode == "coder"))

    if is_private:
        hist, messages = await core.prepare_messages(
            STORE,
            chat_id,
            userbot.DM_HISTORY_LIMIT,
            userbot.GROUP_HISTORY_LIMIT,
            system_fn,
        )
    else:
        label = await userbot.get_sender_label(event)
        user_content = f"{label}: {prompt}" if label else prompt
        await core.append_group_history(
            chat_id,
            user_content,
            chat_history,
            userbot.GROUP_HISTORY_LIMIT,
            ctx_lock,
        )
        hist, messages = await core.prepare_messages(
            STORE,
            chat_id,
            userbot.DM_HISTORY_LIMIT,
            userbot.GROUP_HISTORY_LIMIT,
            system_fn,
        )

    if is_self:
        prefix = f"{text}\n\n"
        self_edit_id = msg_id
    else:
        prefix = ""
        self_edit_id = None

    try:
        full_answer = await core.stream_answer(
            STORE,
            event,
            chat_id,
            is_self,
            messages,
            model,
            prefix,
            self_edit_id,
            core.make_render(
                userbot.render_response, reasoning_hidden, tools_hidden, chat_id
            ),
            edit_text,
            safe_reply,
            cast(Any, get_bot_client().action(chat_id, "typing")),
            userbot.stream_with_tools,
            get_bot_client(),
            tools_module.CODER_TOOLS if coder_active else tools_module.BOT_TOOLS,
            userbot.EDIT_INTERVAL,
            sender_id,
            logger,
            _bot_stats,
        )

        async with ctx_lock:
            hist.append({"role": "assistant", "content": full_answer})
        HISTORY_SAVER.mark_dirty()

        if len(recent_reply_ids) > 5000:
            recent_reply_ids.clear()

    except (userbot.OpenAIError, RPCError, OSError, ValueError, TypeError):
        logger.exception("Бот: ошибка генерации ответа")
        await safe_reply(
            event, "Ошибка при обращении к DanyAPI. / DanyAPI request error."
        )


def _settings_rows(chat_id):
    rows = [
        [Button.inline(f"Модель: {_current_model_label(chat_id)}", b"settings:model")],
        [
            Button.inline(
                f"Рассуждения: {_state_label(chat_id, reasoning_hidden)}",
                b"settings:reasoning",
            )
        ],
        [
            Button.inline(
                f"Инструменты: {_state_label(chat_id, tools_hidden)}", b"settings:tools"
            )
        ],
        [
            Button.inline(
                f"Кодер-режим: {_on_off_label(chat_id, coder_chats)}",
                b"settings:coder",
            )
        ],
        [Button.inline("Очистить контекст", b"settings:clear")],
        [Button.inline("Системный промпт", b"settings:prompt")],
    ]
    return rows


def _model_rows(chat_id):
    current = model_overrides.get(chat_id, userbot.DANYAPI_MODEL)
    rows = []
    for name in userbot.MODELS[:20]:
        marker = " *" if name == current else ""
        rows.append([Button.inline(f"{name}{marker}", _pick_data(name))])
    rows.append([Button.inline("Назад", b"settings:main")])
    return rows


def _pick_data(name: str) -> bytes:
    raw = f"settings:pick:{name}".encode()
    if len(raw) <= CALLBACK_MAX_BYTES:
        return raw
    return f"settings:pick:#{_model_digest(name)}".encode()


def _model_digest(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def _resolve_picked_model(arg: str) -> str:
    if arg.startswith("#"):
        digest = arg[1:]
        for name in userbot.MODELS:
            if _model_digest(name).startswith(digest):
                return name
        return arg
    for name in userbot.MODELS:
        if name == arg:
            return name
    return arg


def _current_model_label(chat_id):
    return model_overrides.get(chat_id, userbot.DANYAPI_MODEL)


def _state_label(chat_id, hidden) -> str:
    return "скрыто" if chat_id in hidden else "видно"


def _on_off_label(chat_id, active) -> str:
    return "вкл" if chat_id in active else "выкл"


def _settings_text(chat_id):
    limit = userbot.DM_HISTORY_LIMIT if chat_id > 0 else userbot.GROUP_HISTORY_LIMIT
    ctx_len = len(chat_history.get(chat_id, deque()))
    return (
        "Настройки / Settings\n"
        f"Модель / Model: {_current_model_label(chat_id)}\n"
        f"Рассуждения / Reasoning: {_state_label(chat_id, reasoning_hidden)}\n"
        f"Инструменты / Tools: {_state_label(chat_id, tools_hidden)}\n"
        f"Кодер-режим / Coder: {_on_off_label(chat_id, coder_chats)}\n"
        f"Контекст / Context: {ctx_len}/{limit}"
    )


async def _answer(event, text=None, alert=False) -> None:
    with contextlib.suppress(RPCError, OSError, ValueError, TypeError, OverflowError):
        await event.answer(text, alert=alert)


async def _edit_settings(event, chat_id, text=None, buttons=None) -> None:
    try:
        await event.edit(
            text if text is not None else _settings_text(chat_id),
            buttons=buttons if buttons is not None else _settings_rows(chat_id),
        )
    except (RPCError, OSError, ValueError, TypeError, OverflowError) as exc:
        logger.debug("Не удалось обновить меню настроек: %r", exc)


async def callback_handler(event: Any):
    chat_id = event.chat_id
    if chat_id is None:
        return
    if event.sender_id not in userbot.OWNER_IDS:
        await _answer(event, "Настройки доступны только владельцу.", alert=True)
        return
    data = event.data
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    parts = str(data or "").split(":", 2)
    action = parts[1].strip() if len(parts) > 1 else ""
    arg = parts[2].strip() if len(parts) > 2 else ""
    if action == "reasoning":
        await _toggle(chat_id, reasoning_hidden)
        save_state()
    elif action == "tools":
        await _toggle(chat_id, tools_hidden)
        save_state()
    elif action == "coder":
        await _toggle(chat_id, coder_chats)
        save_state()
    elif action == "clear":
        limit = userbot.GROUP_HISTORY_LIMIT
        if chat_id > 0:
            limit = userbot.DM_HISTORY_LIMIT
        async with ctx_lock:
            chat_history[chat_id] = deque(maxlen=limit)
        save_history()
    elif action == "pick" and arg:
        async with ctx_lock:
            model_overrides[chat_id] = _resolve_picked_model(arg)
        save_state()
        await _answer(event, "Модель обновлена.")
        await _edit_settings(event, chat_id)
        return
    elif action == "model":
        await _answer(event, "Выбор модели.")
        await _edit_settings(event, chat_id, "Модель / Model:", _model_rows(chat_id))
        return
    elif action == "main":
        await _answer(event)
        await _edit_settings(event, chat_id)
        return
    elif action == "prompt":
        mode = "coder" if coder_active_for(event.sender_id, chat_id) else "bot"
        await safe_reply(
            event,
            userbot.system_prompt_report(
                chat_id, mode=mode, with_contract=(mode == "coder")
            ),
        )
    else:
        await _answer(event, "Неизвестное действие.", alert=True)
        return
    await _answer(event, "Готово.")
    await _edit_settings(event, chat_id)


async def _proxy_candidates():
    try:
        return await proxies.get_proxy_candidates(limit=40)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        logger.warning("Бот: не удалось получить прокси: %r", exc)
        return []


async def start_bot():
    global bot_username, bot_id
    if not userbot.BOT_TOKEN:
        logger.error("ENABLE_BOT=1, но BOT_TOKEN не задан")
        return
    if not userbot.API_ID or not userbot.API_HASH:
        logger.error("ENABLE_BOT=1, но API_ID/API_HASH не заданы в .env")
        return

    load_state()
    load_history()

    cli = get_bot_client()
    cli.add_event_handler(handler, events.NewMessage(incoming=None))
    cli.add_event_handler(callback_handler, events.CallbackQuery())

    candidates = await _proxy_candidates()
    if not candidates:
        logger.warning("Бот: нет прокси, пробую напрямую")
        candidates = [None]

    for idx, proxy in enumerate(candidates):
        if proxy:
            logger.info("Бот: пробую прокси %d/%d: %s", idx + 1, len(candidates), proxy)
            cli.set_proxy(proxy)
        try:
            start_coro = cast(Any, cli.start(bot_token=userbot.BOT_TOKEN))
            await asyncio.wait_for(start_coro, timeout=25)
            me = await cli.get_me()
            bot_username = getattr(me, "username", "") or ""
            bot_id = getattr(me, "id", 0) or 0
            logger.info("Бот запущен как @%s", bot_username or "?")
            try:
                await cli(
                    functions.bots.SetBotCommandsRequest(
                        scope=types.BotCommandScopeDefault(),
                        lang_code="",
                        commands=[
                            types.BotCommand(
                                command="help", description="Справка / Help"
                            ),
                            types.BotCommand(
                                command="clear",
                                description="Очистить контекст / Clear context",
                            ),
                            types.BotCommand(
                                command="settings",
                                description="Настройки / Settings",
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
                await asyncio.wait_for(cast(Any, cli.disconnect()), timeout=10)
            if proxy:
                proxies.mark_bad_proxy(proxies.telethon_to_item(proxy))
    else:
        logger.error("Бот: не удалось подключиться ни через один прокси")
        return

    await cast(Any, cli.run_until_disconnected())


async def disconnect_quietly(timeout=10):
    with contextlib.suppress(Exception):
        await HISTORY_SAVER.flush()
    if bot_client is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(cast(Any, bot_client.disconnect()), timeout=timeout)
