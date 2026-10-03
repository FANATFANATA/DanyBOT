import asyncio
import contextlib
import hashlib
import logging
import re
import socket
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)
from aiogram.utils.token import TokenValidationError
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver
from telethon.errors import FloodWaitError, RPCError

import core
import proxies
import tools as tools_module
import userbot

logger = logging.getLogger("danybot.bot")

handle_bot_commands = core.handle_bot_commands

STATE_FILE = Path(__file__).parent / "state_bot.json"
HISTORY_FILE = Path(__file__).parent / "history_bot.json"
CALLBACK_MAX_BYTES = 64
TYPING_INTERVAL = 4.0
TYPING_ATTEMPTS = 3
CONNECT_TIMEOUT = 25
POLL_TIMEOUT = 30
HTTP_TIMEOUT = max(60, userbot._env_int("BOT_HTTP_TIMEOUT", 300))
PROXY_SCHEMES = {"socks5": "socks5", "socks4": "socks4", "http": "http"}
INLINE_TITLE_LIMIT = 64
INLINE_DESC_LIMIT = 100
INLINE_TEXT_LIMIT = 4000
INLINE_MAX_RESULTS = 20
INLINE_CACHE_TIME = 0
INLINE_SEEN_MAX = 2000
INLINE_SEEN_TTL = 300.0
INLINE_SUGGESTIONS = ("help", "models", "task", "clear")

model_overrides: dict[int, str] = {}
coder_chats: set[int] = set()
reasoning_hidden: set[int] = set()
tools_hidden: set[int] = set()
inline_mode: bool = userbot.INLINE_MODE

chat_history: dict[int, deque] = {}
ctx_lock = asyncio.Lock()
recent_reply_ids: set[tuple[int, int]] = set()
seen_msg_keys: set[tuple[int, int]] = set()
last_chat_activity: dict[int, float] = {}
inline_seen: dict[tuple[int, int], float] = {}
inline_prompts: dict[tuple[int, str], float] = {}

STORE: core.ModeStore = core.ModeStore(globals())
SESSIONS = core.SessionRegistry()
TASKS = core.TaskJournal(lambda: save_state())

bot_username = ""
bot_id = 0


async def _tg_call(awaitable):
    try:
        return await awaitable
    except TelegramRetryAfter as exc:
        raise FloodWaitError(None, capture=max(1, int(exc.retry_after))) from exc
    except TelegramNetworkError as exc:
        raise OSError(str(exc)) from exc
    except TelegramAPIError as exc:
        raise RPCError(request=None, message=str(exc)) from exc


def _sender_id(message) -> int:
    user = getattr(message, "from_user", None)
    if user is None:
        return 0
    return int(user.id)


async def _sender_is_bot(event) -> bool:
    user = getattr(getattr(event, "message", event), "from_user", None)
    if user is None:
        return False
    return bool(getattr(user, "is_bot", False))


def _chat_ref(key) -> int | str:
    if isinstance(key, int):
        return key
    text = str(key).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def _entity_view(view) -> SimpleNamespace:
    return SimpleNamespace(
        id=getattr(view, "id", None),
        title=getattr(view, "title", None),
        username=getattr(view, "username", None),
        first_name=getattr(view, "first_name", "") or "",
        last_name=getattr(view, "last_name", "") or "",
    )


def _menu_commands() -> list[BotCommand]:
    return [
        BotCommand(command="help", description="Справка / Help"),
        BotCommand(command="clear", description="Очистить контекст / Clear context"),
        BotCommand(command="settings", description="Настройки / Settings"),
        BotCommand(command="task", description="Журнал задач / Task log"),
    ]


class TypingAction:
    def __init__(self, bot, chat_id, action_name, interval=TYPING_INTERVAL):
        self._bot = bot
        self._chat_id = chat_id
        self._action = action_name
        self._interval = interval
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *_args) -> bool:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return False

    async def _loop(self) -> None:
        failures = 0
        while True:
            try:
                await _tg_call(self._bot.send_chat_action(self._chat_id, self._action))
                failures = 0
            except (RPCError, OSError, ValueError, TypeError) as exc:
                failures += 1
                logger.debug("Индикатор набора не отправлен: %r", exc)
                if failures >= TYPING_ATTEMPTS:
                    return
            await asyncio.sleep(self._interval)


class SentMessage:
    def __init__(self, message):
        self.id = message.message_id
        self.message = message


class BotMessage:
    def __init__(self, message):
        self._message = message

    @property
    def id(self) -> int:
        return self._message.message_id

    @property
    def message(self) -> str | None:
        return self._message.text

    @property
    def out(self) -> bool:
        return False

    @property
    def from_user(self):
        return getattr(self._message, "from_user", None)

    @property
    def sender_id(self) -> int:
        return _sender_id(self._message)

    @property
    def is_reply(self) -> bool:
        return self._message.reply_to_message is not None

    @property
    def via_bot(self):
        return getattr(self._message, "via_bot", None)

    async def get_reply_message(self):
        replied = self._message.reply_to_message
        if replied is None:
            return None
        return BotMessage(replied)


class BotEvent:
    def __init__(self, message):
        self._message = message
        self.message = BotMessage(message)
        self.chat_id = message.chat.id
        self.sender_id = _sender_id(message)
        self.is_private = getattr(message.chat, "type", "") == "private"

    async def reply(self, text, buttons=None):
        sent = await _tg_call(self._message.answer(text, reply_markup=buttons))
        return SentMessage(sent)

    async def get_sender(self):
        return self._message.from_user


class BotCallbackEvent:
    def __init__(self, query: CallbackQuery):
        self._query = query
        self.data = query.data
        message = query.message
        self.chat_id = message.chat.id if message is not None else None
        self.is_private = (
            getattr(getattr(message, "chat", None), "type", "") == "private"
            if message is not None
            else False
        )
        self.sender_id = _sender_id(query)

    async def answer(self, text=None, alert=False) -> None:
        await _tg_call(self._query.answer(text=text, show_alert=alert))

    async def edit(self, text, buttons=None) -> None:
        message = self._query.message
        if not isinstance(message, Message):
            return
        await _tg_call(message.edit_text(text=text, reply_markup=buttons))

    async def reply(self, text, buttons=None):
        message = self._query.message
        if not isinstance(message, Message):
            return None
        sent = await _tg_call(message.answer(text, reply_markup=buttons))
        return SentMessage(sent)


class BotInlineEvent:
    def __init__(self, query: InlineQuery):
        self._query = query
        self.inline_query_id = query.id
        self.query = (query.query or "").strip()
        self.sender_id = _sender_id(query)


class BotClient:
    def __init__(self, bot):
        self.bot = bot

    async def resolve(self):
        return await self.bot.get_me()

    async def set_commands(self) -> None:
        try:
            await _tg_call(self.bot.set_my_commands(_menu_commands()))
        except (RPCError, OSError, ValueError, TypeError) as exc:
            logger.warning("Не удалось задать меню команд: %s", exc)

    async def answer_inline(self, inline_query_id, results) -> None:
        await _tg_call(
            self.bot.answer_inline_query(
                inline_query_id=inline_query_id,
                results=list(results),
                cache_time=INLINE_CACHE_TIME,
                is_personal=True,
            )
        )

    async def poll(self) -> None:
        await build_dispatcher().start_polling(
            self.bot,
            polling_timeout=POLL_TIMEOUT,
            handle_signals=False,
            close_bot_session=False,
        )

    async def edit_message(self, chat_id, msg_id, text):
        return await _tg_call(
            self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=msg_id)
        )

    def action(self, chat_id, action_name="typing") -> TypingAction:
        return TypingAction(self.bot, chat_id, action_name)

    async def get_me(self) -> SimpleNamespace:
        me = await _tg_call(self.bot.get_me())
        return _entity_view(me)

    async def get_entity(self, key) -> SimpleNamespace:
        entity = _entity_view(await _tg_call(self.bot.get_chat(_chat_ref(key))))
        try:
            entity.participants_count = await _tg_call(
                self.bot.get_chat_member_count(entity.id)
            )
        except (RPCError, OSError, ValueError, TypeError):
            entity.participants_count = None
        return entity

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.bot.session.close()


bot_client: BotClient | None = None
START_TIME = time.monotonic()


def _bot_stats(chat_id):
    return {
        "uptime_seconds": round(time.monotonic() - START_TIME, 1),
        "context_messages": len(chat_history.get(chat_id, deque())),
        "model": model_overrides.get(chat_id, userbot.DANYAPI_MODEL),
    }


def get_bot_client() -> BotClient:
    global bot_client
    if bot_client is None:
        bot_client = BotClient(Bot(token=userbot.BOT_TOKEN, session=_session_for(None)))
    return bot_client


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()

    async def on_message(message: Message) -> None:
        await handler(BotEvent(message))

    async def on_callback(query: CallbackQuery) -> None:
        await callback_handler(BotCallbackEvent(query))

    async def on_inline(query: InlineQuery) -> None:
        await inline_handler(BotInlineEvent(query))

    dispatcher.message.register(on_message)
    dispatcher.callback_query.register(on_callback)
    dispatcher.inline_query.register(on_inline)
    return dispatcher


def _proxy_url(proxy) -> str | None:
    item = proxies.telethon_to_item(proxy)
    if item is None:
        return None
    protocol, host, port = item
    return f"{PROXY_SCHEMES.get(protocol.lower(), 'socks5')}://{host}:{port}"


class _PinnedResolver(AbstractResolver):
    def __init__(self, host: str, address: str) -> None:
        self._host = host.strip().lower()
        self._address = address.strip()
        self._default: AbstractResolver | None = None

    def _fallback(self) -> AbstractResolver:
        if self._default is None:
            self._default = DefaultResolver()
        return self._default

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host.rstrip(".").lower() == self._host and family != socket.AF_INET6:
            return [
                {
                    "hostname": host,
                    "host": self._address,
                    "port": port,
                    "family": socket.AF_INET,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                }
            ]
        return await self._fallback().resolve(host, port, family)

    async def close(self) -> None:
        if self._default is not None:
            await self._default.close()
            self._default = None


def _session_for(proxy, dc=None, address=""):
    url = _proxy_url(proxy)
    kwargs: dict[str, Any] = {"proxy": url} if url else {}
    base = core.dc_api_url(dc)
    if base:
        kwargs["api"] = TelegramAPIServer.from_base(base)
    kwargs["timeout"] = HTTP_TIMEOUT
    session = AiohttpSession(**kwargs)
    pin = address or core.dc_api_pin(dc)
    connector_init = getattr(session, "_connector_init", None)
    if pin and isinstance(connector_init, dict):
        connector_init["resolver"] = _PinnedResolver(core.API_HOST, pin)
    elif pin:
        logger.debug("Сессия aiogram не поддерживает фиксацию адреса ДЦ")
    return session


def _connect(proxy, dc=None, address="") -> BotClient:
    return BotClient(
        Bot(token=userbot.BOT_TOKEN, session=_session_for(proxy, dc, address))
    )


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


NO_TEXT_TRIGGER = False


def _strip_trigger(text, triggered):
    return text.strip()


def _clip(text, limit):
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    return body[: max(1, limit - 1)].rstrip() + "…"


def _prune_inline(now):
    for key, deadline in list(inline_seen.items()):
        if deadline <= now:
            del inline_seen[key]
    core._evict_oldest(inline_seen, INLINE_SEEN_MAX)


def _is_via_own_bot(message) -> bool:
    via = getattr(message, "via_bot", None)
    if via is None:
        return False
    if bot_id and int(getattr(via, "id", 0) or 0) == bot_id:
        return True
    via_username = (getattr(via, "username", None) or "").strip().lower()
    own_username = (bot_username or "").strip().lower()
    return bool(
        via_username
        and own_username
        and (via_username in own_username or own_username in via_username)
    )


def _prune_prompts(now):
    for key, deadline in list(inline_prompts.items()):
        if deadline <= now:
            del inline_prompts[key]
    core._evict_oldest(inline_prompts, INLINE_SEEN_MAX)


def _register_prompt(sender_id, text):
    plain = (text or "").strip()
    if not plain:
        return
    now = time.monotonic()
    _prune_prompts(now)
    inline_prompts[(int(sender_id), plain.lower())] = now + INLINE_SEEN_TTL


def _claim_prompt(sender_id, text) -> bool:
    plain = (text or "").strip()
    if not plain:
        return False
    now = time.monotonic()
    _prune_prompts(now)
    return inline_prompts.pop((int(sender_id), plain.lower()), None) is not None


def _claim_inline(chat_id, msg_id) -> bool:
    key = (int(chat_id), int(msg_id))
    now = time.monotonic()
    _prune_inline(now)
    if key in inline_seen:
        return False
    inline_seen[key] = now + INLINE_SEEN_TTL
    return True


BOT_HELP_TEXT = (
    "DanyBOT - команды / commands:\n"
    "/help /start - справка / help\n"
    "/clear - очистить контекст / clear context\n"
    "/settings - настройки, инлайн-меню / settings, inline menu\n"
    "/task - журнал последней задачи / last task log\n\n"
    "Модель, рассуждения, инструменты, кодер-режим и системный\n"
    "промпт - в меню /settings.\n\n"
    "Также работает / Also works: @упоминание, реплай боту.\n"
    "Инлайн-режим: набери @бота в любом чате.\n"
    "Inline mode: type @bot in any chat."
)


def load_state():
    core.load_state_into(STORE, STATE_FILE, logger, journal=TASKS)


def save_state():
    core.save_state_from(STORE, STATE_FILE, logger, journal=TASKS)


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

    if (chat_id, msg_id) in recent_reply_ids:
        return

    is_private = event.is_private
    if is_private and await _sender_is_bot(event):
        return
    triggered = NO_TEXT_TRIGGER
    via_msg = getattr(message, "via_bot", None)
    via_inline = _is_via_own_bot(message)
    if via_inline:
        if _claim_inline(chat_id, msg_id):
            triggered = True
            _claim_prompt(sender_id, text)
            logger.info("Бот: инлайн-запрос из чата %s от %s", chat_id, sender_id)
    elif via_msg is not None:
        logger.debug("Бот: инлайн через чужой бот %r, игнорирую", via_msg)
    elif _claim_prompt(sender_id, text):
        triggered = True
        logger.info("Бот: инлайн-запрос (текст) из чата %s от %s", chat_id, sender_id)
    mentioned = _is_mentioned(text)
    now = time.monotonic()

    reply_to_bot = False
    replied_text = None
    if message.is_reply and not is_self:
        reply_msg = await core.fetch_replied_message(message)
        if reply_msg is not None:
            if getattr(reply_msg, "out", False) or (
                bot_id and getattr(reply_msg, "sender_id", None) == bot_id
            ):
                reply_to_bot = True
            if reply_msg.message:
                replied_text = reply_msg.message.strip()

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
            return
        SESSIONS.cancel(chat_id, reason="кодер выключен", logger=logger)
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
            await event.reply(
                _settings_text(chat_id, is_private), buttons=_settings_rows(chat_id)
            )
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
        if command[0] in core.OWNER_COMMANDS and sender_id not in userbot.OWNER_IDS:
            await safe_reply(event, "Команда доступна только владельцу.")
            return
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
            task_text=TASKS.report(chat_id),
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
        _strip_trigger,
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

    limit = userbot.MAX_REQUEST_LEN
    prompt = core.compose_prompt(_strip_mention(text.strip()), replied_text, limit)

    if not prompt:
        return

    if len(prompt) > limit:
        prompt = prompt[:limit]

    model = model_overrides.get(chat_id, userbot.DANYAPI_MODEL)
    mode = "coder" if coder_active else "bot"
    is_owner = sender_id in userbot.OWNER_IDS

    def system_fn(_cid):
        return userbot.system_for(_cid, mode=mode, with_contract=(mode == "coder"))

    if is_private:
        messages = await core.prepare_messages(
            STORE,
            chat_id,
            userbot.DM_HISTORY_LIMIT,
            userbot.GROUP_HISTORY_LIMIT,
            system_fn,
            is_private=True,
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
        HISTORY_SAVER.mark_dirty()
        messages = await core.prepare_messages(
            STORE,
            chat_id,
            userbot.DM_HISTORY_LIMIT,
            userbot.GROUP_HISTORY_LIMIT,
            system_fn,
            is_private=False,
        )

    if is_self:
        prefix = f"{text}\n\n"
        self_edit_id = msg_id
    else:
        prefix = ""
        self_edit_id = None

    delivery: dict[str, bool] = {}
    if coder_active:
        tool_menu = tools_module.CODER_TOOLS
    elif is_owner:
        tool_menu = tools_module.BOT_TOOLS
    else:
        tool_menu = tools_module.PUBLIC_TOOLS
    limit_ctx = userbot.DM_HISTORY_LIMIT if is_private else userbot.GROUP_HISTORY_LIMIT
    progress: dict[str, str] = {"reason": ""}
    task_token: list[Any] = [None]

    def on_progress(rounds, calls_made, reason=""):
        if reason:
            progress["reason"] = reason
        TASKS.progress(chat_id, rounds=rounds, tools=calls_made, token=task_token[0])

    async def generate():
        record = TASKS.begin(
            chat_id, prompt, model=model, coder=coder_active, owner=is_owner
        )
        task_token[0] = record.get("token")
        try:
            answer = await core.stream_answer(
                STORE,
                event=event,
                chat_id=chat_id,
                is_self=is_self,
                messages=messages,
                model=model,
                prefix=prefix,
                self_edit_id=self_edit_id,
                render_fn=core.make_render(
                    userbot.render_response, reasoning_hidden, tools_hidden, chat_id
                ),
                edit_fn=edit_text,
                reply_fn=safe_reply,
                action=get_bot_client().action(chat_id, "typing"),
                stream_fn=userbot.stream_with_tools,
                tool_client=get_bot_client(),
                tools=tool_menu,
                edit_interval=userbot.EDIT_INTERVAL,
                owner_id=sender_id,
                logger=logger,
                stats=_bot_stats,
                delivery=delivery,
                progress_fn=on_progress,
            )
        except asyncio.CancelledError:
            TASKS.finish(
                chat_id,
                core.TASK_INTERRUPTED,
                reason="прерван новым запросом",
                token=record.get("token"),
            )
            raise
        except BaseException as exc:
            TASKS.finish(
                chat_id,
                core.TASK_FAILED,
                reason=type(exc).__name__,
                token=record.get("token"),
            )
            raise
        TASKS.finish(
            chat_id,
            core.TASK_STOPPED if progress["reason"] else core.TASK_DONE,
            reason=progress["reason"],
            token=record.get("token"),
        )
        if answer.strip():
            async with ctx_lock:
                chat_history.setdefault(chat_id, deque(maxlen=limit_ctx)).append(
                    {"role": "assistant", "content": answer}
                )
            HISTORY_SAVER.mark_dirty()
        return answer

    task = SESSIONS.start(
        chat_id, generate(), logger=logger, scope="owner" if is_owner else "other"
    )
    try:
        await task
    except asyncio.CancelledError:
        if core.is_own_cancellation():
            raise
        logger.info("Бот: запрос в чате %s прерван, жду новый", chat_id)
    except userbot.HANDLER_ERRORS:
        logger.exception("Бот: ошибка генерации ответа")
        if not delivery.get("delivered"):
            await safe_reply(event, core.ERROR_NOTICE)


def _settings_rows(chat_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Модель: {_current_model_label(chat_id)}",
                    callback_data="settings:model",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Рассуждения: {_state_label(chat_id, reasoning_hidden)}",
                    callback_data="settings:reasoning",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Инструменты: {_state_label(chat_id, tools_hidden)}",
                    callback_data="settings:tools",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Кодер-режим: {_on_off_label(chat_id, coder_chats)}",
                    callback_data="settings:coder",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Инлайн-режим: {_global_on_off_label(inline_mode)}",
                    callback_data="settings:inline",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Очистить контекст", callback_data="settings:clear"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Системный промпт", callback_data="settings:prompt"
                )
            ],
        ]
    )


MODEL_MENU_LIMIT = 20


def _model_rows(chat_id) -> InlineKeyboardMarkup:
    current = model_overrides.get(chat_id, userbot.DANYAPI_MODEL)
    rows = []
    for name in userbot.MODELS[:MODEL_MENU_LIMIT]:
        marker = " *" if name == current else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{name}{marker}", callback_data=_pick_data(name)
                )
            ]
        )
    hidden = len(userbot.MODELS) - MODEL_MENU_LIMIT
    if hidden > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Ещё {hidden} (список /models)",
                    callback_data="settings:models",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="settings:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pick_data(name: str) -> str:
    raw = f"settings:pick:{name}"
    if len(raw.encode("utf-8")) <= CALLBACK_MAX_BYTES:
        return raw
    return f"settings:pick:#{_model_digest(name)}"


def _model_digest(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def _resolve_picked_model(arg: str) -> str | None:
    if arg.startswith("#"):
        digest = arg[1:]
        for name in userbot.MODELS:
            if _model_digest(name).startswith(digest):
                return name
        return None
    for name in userbot.MODELS:
        if name == arg:
            return name
    return None


def _current_model_label(chat_id):
    return model_overrides.get(chat_id, userbot.DANYAPI_MODEL)


def _state_label(chat_id, hidden) -> str:
    return "скрыто" if chat_id in hidden else "видно"


def _on_off_label(chat_id, active) -> str:
    return "вкл" if chat_id in active else "выкл"


def _global_on_off_label(active) -> str:
    return "вкл" if active else "выкл"


def _settings_text(chat_id, is_private=None):
    limit = core.history_limit(
        chat_id, is_private, userbot.DM_HISTORY_LIMIT, userbot.GROUP_HISTORY_LIMIT
    )
    ctx_len = len(chat_history.get(chat_id, deque()))
    return (
        "Настройки / Settings\n"
        f"Модель / Model: {_current_model_label(chat_id)}\n"
        f"Рассуждения / Reasoning: {_state_label(chat_id, reasoning_hidden)}\n"
        f"Инструменты / Tools: {_state_label(chat_id, tools_hidden)}\n"
        f"Кодер-режим / Coder: {_on_off_label(chat_id, coder_chats)}\n"
        f"Инлайн-режим / Inline: {_global_on_off_label(inline_mode)}\n"
        f"Контекст / Context: {ctx_len}/{limit}\n"
        f"Задача / Task: {core.task_state_label(TASKS.get(chat_id))}"
    )


async def _answer(event, text=None, alert=False) -> None:
    with contextlib.suppress(RPCError, OSError, ValueError, TypeError, OverflowError):
        await event.answer(text, alert=alert)


async def _edit_settings(event, chat_id, text=None, buttons=None) -> None:
    try:
        await event.edit(
            text if text is not None else _settings_text(chat_id, event.is_private),
            buttons if buttons is not None else _settings_rows(chat_id),
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
        if chat_id not in coder_chats:
            SESSIONS.cancel(chat_id, reason="кодер выключен", logger=logger)
    elif action == "inline":
        global inline_mode
        inline_mode = not inline_mode
        save_state()
    elif action == "clear":
        limit = core.history_limit(
            chat_id,
            event.is_private,
            userbot.DM_HISTORY_LIMIT,
            userbot.GROUP_HISTORY_LIMIT,
        )
        async with ctx_lock:
            core.history_for(chat_history, chat_id, limit).clear()
        save_history()
    elif action == "pick" and arg:
        picked = _resolve_picked_model(arg)
        if picked is None:
            await _answer(event, "Модель больше недоступна.", alert=True)
            await _edit_settings(event, chat_id)
            return
        async with ctx_lock:
            model_overrides[chat_id] = picked
        save_state()
        await _answer(event, "Модель обновлена.")
        await _edit_settings(event, chat_id)
        return
    elif action == "model":
        await _answer(event, "Выбор модели.")
        await _edit_settings(event, chat_id, "Модель / Model:", _model_rows(chat_id))
        return
    elif action == "models":
        await _answer(event)
        await safe_reply(
            event,
            core.models_text(
                model_overrides.get(chat_id, userbot.DANYAPI_MODEL), userbot.MODELS
            ),
        )
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


def _inline_article(article_id, title, text, sender_id=None):
    body = _clip(text, INLINE_TEXT_LIMIT)
    if sender_id is not None:
        _register_prompt(sender_id, body)
    return InlineQueryResultArticle(
        id=article_id,
        title=_clip(title, INLINE_TITLE_LIMIT),
        description=_clip(text, INLINE_DESC_LIMIT),
        input_message_content=InputTextMessageContent(
            message_text=body,
            disable_web_page_preview=True,
        ),
    )


def _inline_available(sender_id):
    return int(sender_id) in userbot.OWNER_IDS


def _inline_suggestions(sender_id):
    owner = _inline_available(sender_id)
    return [
        _inline_article(
            f"cmd:{name}",
            core.BOT_COMMAND_TITLES.get(name, name),
            f"/{name}",
            sender_id,
        )
        for name in INLINE_SUGGESTIONS
        if owner or name not in core.INLINE_OWNER_COMMANDS
    ]


def build_inline_results(query, sender_id):
    text = (query or "").strip()
    commands = core.inline_command_matches(text, _inline_available(sender_id))
    if commands:
        return [
            _inline_article(f"cmd:{cmd[1:]}", title, cmd, sender_id)
            for cmd, title in commands[:INLINE_MAX_RESULTS]
        ]
    if text:
        return [_inline_article("ask", userbot.BOT_NAME, text, sender_id)]
    return _inline_suggestions(sender_id)


async def inline_handler(event: Any):
    results = build_inline_results(event.query, event.sender_id) if inline_mode else []
    client = bot_client or get_bot_client()
    last = None
    for attempt in range(3):
        try:
            await client.answer_inline(event.inline_query_id, results)
            return
        except (
            TelegramAPIError,
            RPCError,
            OSError,
            ValueError,
            TypeError,
            OverflowError,
        ) as exc:
            last = exc
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
    logger.warning("Инлайн-запрос не обработан после повторов: %r", last)


async def _proxy_candidates():
    try:
        return await proxies.get_proxy_candidates(limit=40)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        logger.warning("Бот: не удалось получить прокси: %r", exc)
        return []


async def _close_client(client) -> None:
    if client is not None:
        await client.close()


async def _connect_all(dc, candidates):
    for idx, proxy in enumerate(candidates):
        if proxy:
            logger.info(
                "Бот: ДЦ %s, прокси %d/%d: %s",
                dc["address"],
                idx + 1,
                len(candidates),
                proxy,
            )
        attempt = None
        try:
            attempt = _connect(proxy, dc["dc"] or None, dc["address"])
            me = await asyncio.wait_for(attempt.resolve(), timeout=CONNECT_TIMEOUT)
        except (TelegramUnauthorizedError, TokenValidationError) as exc:
            logger.error("Токен бота невалиден: %s", exc)
            await _close_client(attempt)
            return None, True
        except (
            TelegramAPIError,
            TelegramNetworkError,
            RPCError,
            FloodWaitError,
            OSError,
            TimeoutError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning(
                "Бот: не удалось подключиться через %s (%s): %s",
                proxy,
                dc["address"],
                type(exc).__name__,
            )
            await _close_client(attempt)
            if proxy:
                proxies.mark_bad_proxy(proxies.telethon_to_item(proxy))
            continue
        global bot_username, bot_id
        bot_username = getattr(me, "username", "") or ""
        bot_id = int(getattr(me, "id", 0) or 0)
        return attempt, False
    return None, False


async def start_bot():
    global bot_client
    if not userbot.BOT_TOKEN:
        logger.error("ENABLE_BOT=1, но BOT_TOKEN не задан")
        return

    load_state()
    load_history()

    candidates = await _proxy_candidates()
    if not candidates:
        logger.warning("Бот: нет прокси, пробую напрямую")
        candidates = [None]

    client = None
    for dc in core.dc_candidates():
        client, fatal = await _connect_all(dc, candidates)
        if fatal:
            return
        if client is not None:
            break
        logger.warning("Бот: ДЦ %s недоступен, пробую следующий", dc["address"])

    if client is None:
        logger.error("Бот: не удалось подключиться ни через один ДЦ и прокси")
        return

    bot_client = client
    await client.set_commands()
    logger.info("Бот запущен как @%s (id %s)", bot_username or "?", bot_id)
    if inline_mode:
        logger.info(
            "Инлайн-режим включён: набери @%s в любом чате. "
            "Если бота нет в списке, выполни /setinline у @BotFather",
            bot_username or "bot",
        )
    else:
        logger.info("Инлайн-режим выключен в настройках бота")
    await client.poll()


async def disconnect_quietly(timeout=10):
    SESSIONS.cancel_all(reason="остановка", logger=logger)
    await SESSIONS.drain(timeout)
    inline_seen.clear()
    inline_prompts.clear()
    with contextlib.suppress(Exception):
        await HISTORY_SAVER.flush()
    if bot_client is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(bot_client.close(), timeout=timeout)
