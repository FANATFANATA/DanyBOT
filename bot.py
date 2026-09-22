import asyncio
import contextlib
import html
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

from telethon import TelegramClient, events
from telethon.errors import AuthKeyError, RPCError
from telethon.sessions import StringSession
from telethon.tl import functions, types

import core
import proxies
import userbot

logger = logging.getLogger("danybot.bot")

BOT_COMMANDS = core.BOT_COMMANDS
handle_bot_commands = core.handle_bot_commands

STATE_FILE = Path(__file__).parent / "state_bot.json"
HISTORY_FILE = Path(__file__).parent / "history_bot.json"

model_overrides: dict[int, str] = {}
auto_respond: set[int] = set()
ignored_chats: set[int] = set()
ignored_users: set[int] = set()

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


bot_client = None


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
    core.load_state_into(STORE, STATE_FILE)


def save_state():
    core.save_state_from(STORE, STATE_FILE, logger)


def load_history():
    core.load_history_into(
        STORE, HISTORY_FILE, userbot.DM_HISTORY_LIMIT, userbot.GROUP_HISTORY_LIMIT
    )


def save_history():
    core.save_history_from(STORE, HISTORY_FILE, logger)


HISTORY_SAVER = core.AsyncSaver(save_history, delay=0.5, logger=logger)


async def safe_reply(event, text):
    return await core.safe_reply(event, text, userbot.REPLY_ATTEMPTS, recent_reply_ids)


async def edit_text(chat_id, msg_id, text):
    return await core.edit_text(
        get_bot_client(), chat_id, msg_id, text, userbot.REPLY_ATTEMPTS
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
    # Бот не реагирует на "."-алиасы юзербота — только /команды, упоминание,
    # реплай и личка.
    triggered = False
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

    if command and command[0] in ("ignore", "unignore"):
        return

    if command:
        resp = core.handle_command_state(
            command,
            chat_id,
            is_private,
            chat_history,
            model_overrides,
            auto_respond,
            ignored_chats,
            ignored_users,
            userbot.DM_HISTORY_LIMIT,
            userbot.GROUP_HISTORY_LIMIT,
            userbot.AUTO_RESPOND_GLOBAL,
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

    if triggered or mentioned or reply_to_bot or (is_private and not is_self):
        effective_trigger = True
    elif not is_self:
        effective_trigger = chat_id in auto_respond or userbot.AUTO_RESPOND_GLOBAL
    else:
        effective_trigger = False

    if not effective_trigger:
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
    if is_private:
        hist, messages = await core.prepare_messages(
            STORE,
            chat_id,
            userbot.DM_HISTORY_LIMIT,
            userbot.GROUP_HISTORY_LIMIT,
            lambda cid: userbot.system_for(cid, mode="bot"),
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
            lambda cid: userbot.system_for(cid, mode="bot"),
        )

    if is_self:
        prefix = f"{html.escape(text)}\n\n"
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
            userbot.render_response,
            edit_text,
            safe_reply,
            cast(Any, get_bot_client().action(chat_id, "typing")),
            userbot.stream_with_tools,
            get_bot_client(),
            userbot.TOOLS,
            userbot.EDIT_INTERVAL,
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

    candidates = await proxies.get_proxy_candidates(limit=40)
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
                await asyncio.wait_for(cast(Any, cli.disconnect()), timeout=10)
            if proxy:
                proxies.mark_bad_proxy(proxies.telethon_to_item(proxy))
    else:
        logger.error("Бот: не удалось подключиться ни через один прокси")
        return

    await cast(Any, cli.run_until_disconnected())


async def disconnect_quietly(timeout=10):
    if bot_client is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(cast(Any, bot_client.disconnect()), timeout=timeout)
