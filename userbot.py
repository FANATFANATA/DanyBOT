import asyncio
import contextlib
import html
import json
import logging
import os
import re
import struct
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError
from telethon import TelegramClient, events
from telethon.errors import AuthKeyError, RPCError
from telethon.sessions import StringSession

import core
import proxies
import tools as tools_module

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("danybot")

_env_str = core._env_str
_env_int = core._env_int
_env_float = core._env_float
_env_bool = core._env_bool
handle_commands = core.handle_commands


def strip_role_tag(text: str) -> str:
    return core.strip_role_tag(text, BOT_NAME)


API_ID = _env_int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_NAME = os.getenv("SESSION_NAME", "session")

DANYAPI_URL = os.getenv("DANYAPI_URL", "http://127.0.0.1:8008/v1")
DANYAPI_MODEL = os.getenv("DANYAPI_MODEL", "deepseek-v4.1-flash")
DANYAPI_KEY = os.getenv("DANYAPI_KEY", "danyapi")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — DanyBOT, юзербот пользователя DanyaVoredom, работающий в Telegram. "
    "Отвечай максимально кратко и только по делу.",
)
SYSTEM_PROMPT_BOT = os.getenv(
    "SYSTEM_PROMPT_BOT",
    "Ты — DanyBOT, Telegram-бот. Отвечай максимально кратко и только по делу. "
    "Всегда отвечай на том языке, на котором написано последнее сообщение "
    "пользователя.",
)
TOOL_VERIFY_PROMPT = os.getenv(
    "TOOL_VERIFY_PROMPT",
    "Ты — система безопасности Telegram-бота. Тебе показывают вызов инструмента "
    "с аргументами. Оцени, безопасно ли его выполнять: не приведёт ли он к удалению "
    "или порче данных, утечке приватной информации, выполнению опасных shell-команд, "
    "рассылке сообщений, действиям против владельца аккаунта. "
    "Ответь строго одним словом: ALLOW или DENY.",
)
TOOL_VERIFY_MODEL = _env_str("TOOL_VERIFY_MODEL", "")
RUN_SHELL_MODEL = _env_str("RUN_SHELL_MODEL", "")
RUN_SHELL_VERIFY_PROMPT = os.getenv(
    "RUN_SHELL_VERIFY_PROMPT",
    "Ты — система безопасности Telegram-бота. Тебе показывают вызов инструмента "
    "run_shell с shell-командой. Сначала рассуждай по шагам: что именно выполнит "
    "команда, какие файлы/данные затронет, есть ли удаление, перезапись, эксфильтрация "
    "секретов, обращение к сети, повышение прав, действия против владельца аккаунта. "
    "Затем на последней строке ответь строго одним словом: ALLOW или DENY.",
)
SANITIZE_ENABLED = _env_bool("SANITIZE_ENABLED", True)
SANITIZE_MODEL = _env_str("SANITIZE_MODEL", "")
SANITIZE_PROMPT = os.getenv(
    "SANITIZE_PROMPT",
    "Ты — фильтр секретов. Тебе дают вывод shell-команды. Сначала рассуждай по шагам, "
    "затем верни ТОЛЬКО очищенный текст: удали или замени на [REDACTED] приватные "
    "данные — значения из .env и любых конфигов, токены, API-ключи, пароли, хеши, "
    "cookie, приватные ключи, строки сессий, URL с credentials. Остальной текст "
    "сохрани дословно. Не добавляй пояснений, верни только очищенный вывод.",
)

TRIGGER_ALIASES = core.TRIGGER_ALIASES
AUTO_ALIASES = core.AUTO_ALIASES
TRIGGER_RE = core.TRIGGER_RE

EDIT_INTERVAL = max(0.2, _env_float("EDIT_INTERVAL", 1.0))
GROUP_HISTORY_LIMIT = max(2, _env_int("GROUP_HISTORY_LIMIT", 40))
DM_HISTORY_LIMIT = max(2, _env_int("DM_HISTORY_LIMIT", 100))
MAX_TOKENS = max(64, _env_int("MAX_TOKENS", 4096))
MAX_REQUEST_LEN = max(100, _env_int("MAX_REQUEST_LEN", 8000))
MAX_TOOL_ROUNDS = max(1, _env_int("MAX_TOOL_ROUNDS", 8))
AUTO_RESPOND_GLOBAL = _env_str("AUTO_RESPOND", "0").lower() in ("1", "true", "yes")
COOLDOWN = max(0.0, _env_float("COOLDOWN", 0.0))
BOT_NAME = _env_str("BOT_NAME", "DanyBOT")
SYSTEM_PROMPT_FILE = _env_str("SYSTEM_PROMPT_FILE", "")

ENABLE_USERBOT = _env_bool("ENABLE_USERBOT", True)
ENABLE_BOT = _env_bool("ENABLE_BOT", False)
BOT_TOKEN = _env_str("BOT_TOKEN", "")


def _env_pos_int(name):
    raw = _env_str(name, "")
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


SUBAGENT_ENABLED = _env_bool("SUBAGENT_ENABLED", True)
SUBAGENT_MODEL = _env_str("SUBAGENT_MODEL", "")
SUBAGENT_MAX_ROUNDS = _env_pos_int("SUBAGENT_MAX_ROUNDS")
SUBAGENT_CONCURRENCY = _env_pos_int("SUBAGENT_CONCURRENCY")

REPLY_ATTEMPTS = 3


def _read_extra_system(path):
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Не удалось прочитать SYSTEM_PROMPT_FILE: %s", exc)
        return ""


EXTRA_SYSTEM = _read_extra_system(SYSTEM_PROMPT_FILE)

STATE_FILE = Path(__file__).parent / "state_userbot.json"
HISTORY_FILE = Path(__file__).parent / "history_userbot.json"

MODELS: list[str] = ["deepseek-v4-flash"]

model_overrides: dict[int, str] = {}
auto_respond: set[int] = set()
ignored_chats: set[int] = set()
ignored_users: set[int] = set()

chat_history: dict[int, deque] = {}
ctx_lock = asyncio.Lock()
recent_reply_ids: set[int] = set()
seen_msg_keys: set[tuple[int, int]] = set()
last_chat_activity: dict[int, float] = {}
START_TIME = time.monotonic()

STORE: core.ModeStore = core.ModeStore(globals())


def make_session(name: str):
    value = name.strip()
    if value.startswith("1"):
        try:
            return StringSession(value)
        except (ValueError, TypeError, struct.error):
            return name
    return name


client = None


def get_client():
    global client
    if client is None:
        client = TelegramClient(
            make_session(SESSION_NAME),
            API_ID,
            API_HASH,
            connection_retries=2,
            request_retries=1,
            retry_delay=0,
            timeout=10,
        )
    return client


ai = AsyncOpenAI(base_url=DANYAPI_URL, api_key=DANYAPI_KEY)


def load_state():
    core.load_state_into(STORE, STATE_FILE)


def save_state():
    core.save_state_from(STORE, STATE_FILE, logger)


def load_history():
    core.load_history_into(STORE, HISTORY_FILE, DM_HISTORY_LIMIT, GROUP_HISTORY_LIMIT)


def save_history():
    core.save_history_from(STORE, HISTORY_FILE, logger)


HISTORY_SAVER = core.AsyncSaver(save_history, delay=0.5, logger=logger)


def model_for(chat_id):
    return model_overrides.get(chat_id, DANYAPI_MODEL)


def system_for(chat_id, mode="userbot"):
    base = SYSTEM_PROMPT_BOT if mode == "bot" else SYSTEM_PROMPT
    if EXTRA_SYSTEM:
        return f"{base}\n\n{EXTRA_SYSTEM}"
    return base


TOOLS = tools_module.TOOLS
safe_eval = tools_module.safe_eval
render_response = tools_module.render_response


def _bot_stats(chat_id):
    uptime = time.monotonic() - START_TIME
    return {
        "uptime_seconds": round(uptime, 1),
        "python": sys.version.split()[0],
        "context_messages": len(chat_history.get(chat_id, deque())),
        "model": model_for(chat_id),
        "modes": {"userbot": ENABLE_USERBOT, "bot": ENABLE_BOT},
    }


async def execute_tool(name: str, arguments: dict, chat_id, client=None):
    if client is None:
        client = globals()["client"]
    return await tools_module.execute_tool(name, arguments, chat_id, client, _bot_stats)


def _last_decision(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        matches = re.findall(r"\b(ALLOW|DENY)\b", stripped.upper())
        if matches:
            return matches[-1]
    return ""


async def verify_tool_call(name: str, arguments: dict, model: str) -> bool:
    payload = json.dumps({"tool": name, "arguments": arguments}, ensure_ascii=False)
    if name == "run_shell":
        prompt = RUN_SHELL_VERIFY_PROMPT
        use_model = RUN_SHELL_MODEL or model
        max_tokens = 1024
    else:
        prompt = TOOL_VERIFY_PROMPT
        use_model = model
        max_tokens = 8
    try:
        resp = await ai.chat.completions.create(
            model=use_model,
            messages=cast(
                Any,
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": payload},
                ],
            ),
            temperature=0,
            max_tokens=max_tokens,
            stream=False,
        )
    except (OpenAIError, OSError, ValueError, TypeError):
        return False
    try:
        message = resp.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return False

    raw_content = getattr(message, "content", None)
    raw_reasoning = getattr(message, "reasoning_content", None)
    if isinstance(raw_content, list):
        content = "".join(str(x) for x in raw_content).strip()
    else:
        content = (raw_content or "").strip()
    reasoning = (raw_reasoning or "").strip()
    if reasoning:
        content = f"{reasoning}\n{content}".strip()
    decision = _last_decision(content)
    if decision != "ALLOW":
        logger.info(
            "Верификация %s: %s | ответ модели: %r",
            name,
            decision or "нет решения",
            content[:300],
        )
        return False
    return True


async def sanitize_tool_output(output: str, model: str) -> str:
    if not SANITIZE_ENABLED or not output:
        return output
    use_model = SANITIZE_MODEL or model
    try:
        resp = await ai.chat.completions.create(
            model=use_model,
            messages=cast(
                Any,
                [
                    {"role": "system", "content": SANITIZE_PROMPT},
                    {"role": "user", "content": output},
                ],
            ),
            temperature=0,
            max_tokens=MAX_TOKENS,
            stream=False,
        )
    except (OpenAIError, OSError, ValueError, TypeError):
        return "[вывод скрыт: ошибка санитайзера]"
    try:
        message = resp.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return "[вывод скрыт: пустой ответ санитайзера]"
    raw_content = getattr(message, "content", None)
    raw_reasoning = getattr(message, "reasoning_content", None)
    if isinstance(raw_content, list):
        content = "".join(str(x) for x in raw_content).strip()
    else:
        content = (raw_content or "").strip()
    reasoning = (raw_reasoning or "").strip()
    if reasoning:
        content = f"{reasoning}\n{content}".strip()
    content = content.strip()
    if not content:
        return "[вывод скрыт: пустой ответ санитайзера]"
    return content


async def stream_with_tools(
    messages: list,
    model: str,
    chat_id,
    on_delta,
    on_reasoning,
    on_tool=None,
    client_override=None,
    tools=None,
    verify_tools=False,
):
    working: list[dict[str, Any]] = [dict(m) for m in messages]
    rounds = 0
    content_parts: list[str] = []
    while True:
        rounds += 1
        if rounds > MAX_TOOL_ROUNDS:
            return (
                "".join(content_parts)
                if content_parts
                else "Достигнут лимит циклов инструментов."
            )
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts = []
        raw_stream = await ai.chat.completions.create(
            model=model,
            messages=cast(Any, working),
            temperature=0.7,
            max_tokens=MAX_TOKENS,
            stream=True,
            tools=cast(Any, tools if tools is not None else TOOLS),
        )
        stream = cast(Any, raw_stream)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta:
                reasoning = getattr(delta, "reasoning_content", None)
                if not reasoning:
                    reasoning = getattr(delta, "reasoning", None)
                if reasoning:
                    await on_reasoning(reasoning)
            if delta and delta.content:
                content_parts.append(delta.content)
                await on_delta(delta.content)
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = tool_calls.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
        if not tool_calls:
            return "".join(content_parts)
        working.append(
            {
                "role": "assistant",
                "content": "".join(content_parts),
                "tool_calls": [
                    {
                        "id": slot["id"],
                        "type": "function",
                        "function": {
                            "name": slot["name"],
                            "arguments": slot["arguments"] or "{}",
                        },
                    }
                    for _idx, slot in sorted(tool_calls.items())
                ],
            }
        )
        slots = [slot for _idx, slot in sorted(tool_calls.items())]

        async def run_slot(slot):
            try:
                args = json.loads(slot["arguments"] or "{}")
            except (json.JSONDecodeError, ValueError):
                args = {}
            verify_model = TOOL_VERIFY_MODEL or model
            if verify_tools and not await verify_tool_call(
                slot["name"], args, verify_model
            ):
                return slot, None
            if client_override is None:
                result = await execute_tool(slot["name"], args, chat_id)
            else:
                result = await execute_tool(
                    slot["name"], args, chat_id, client_override
                )
            if slot["name"] == "run_shell":
                result = await sanitize_tool_output(result, model)
            return slot, result

        results = await asyncio.gather(*(run_slot(s) for s in slots))
        for slot, result in results:
            if on_tool is not None:
                await on_tool(slot["name"])
            if result is None:
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": slot["id"],
                        "content": "Вызов отклонён проверкой безопасности.",
                    }
                )
                continue
            working.append(
                {
                    "role": "tool",
                    "tool_call_id": slot["id"],
                    "content": result,
                }
            )


async def get_sender_label(event):
    try:
        sender = await event.get_sender()
    except (RPCError, OSError, ValueError):
        sender = None
    if not sender:
        return str(event.sender_id)
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    username = getattr(sender, "username", None)
    full = " ".join(x for x in [first, last] if x).strip()
    if username:
        return f"{full} (@{username})" if full else f"@{username}"
    return full or str(event.sender_id)


async def safe_reply(event, text):
    return await core.safe_reply(event, text, REPLY_ATTEMPTS, recent_reply_ids)


async def edit_text(chat_id, msg_id, text):
    return await core.edit_text(get_client(), chat_id, msg_id, text, REPLY_ATTEMPTS)


def models_text(chat_id) -> str:
    current = model_for(chat_id)
    return core.models_text(current, MODELS)


async def refresh_models():
    global MODELS
    try:
        models = await ai.models.list()
        ids = [m.id for m in models.data if getattr(m, "id", None)]
        if ids:
            MODELS = ids
            logger.info("Загружено %d моделей из DanyAPI", len(ids))
    except (OpenAIError, OSError, ValueError) as exc:
        logger.warning("Не удалось загрузить модели из DanyAPI: %s", exc)


HELP_TEXT = (
    "Алиасы триггера / Trigger aliases: .danybot .danyapi .dany .db .gpt .ai "
    ".бот .д .б\n"
    "Алиасы команд / Command aliases: help/помощь, model/модель, "
    "models/модели, clear/забудь/сброс/очистить, history/история, ping\n\n"
    "Команды / Commands:\n"
    ".danybot <текст/text> — вопрос / question\n"
    ".danybot model <id> — сменить модель / set model\n"
    ".danybot models — список моделей / list models\n"
    ".danybot model — показать текущую модель / show current model\n"
    ".danybot clear — очистить контекст / clear context\n"
    ".danybot history — размер контекста / context size\n"
    ".danybot ping — статус / status\n"
    ".danybot ignore / unignore — заглушить/разглушить чат / mute/unmute "
    "chat\n"
    ".danybot help — эта справка / this help\n\n"
    "Авто-ответ / Auto-reply: .danyauto on/off (.da .auto .авто)"
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
    triggered = bool(TRIGGER_RE.search(text))
    now = time.monotonic()

    try:
        command = handle_commands(text)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        command = None

    if command and command[0] == "unignore":
        async with ctx_lock:
            ignored_chats.discard(chat_id)
        save_state()
        await safe_reply(event, "Чат разглушен. / Chat unmuted.")
        return

    if chat_id in ignored_chats:
        return
    if sender_id is not None and sender_id in ignored_users:
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
            DM_HISTORY_LIMIT,
            GROUP_HISTORY_LIMIT,
            AUTO_RESPOND_GLOBAL,
            DANYAPI_MODEL,
            MODELS,
            HELP_TEXT,
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
        lambda t, tr: TRIGGER_RE.sub("", t, count=1).strip() if tr else t.strip(),
        strip_role_tag,
        lambda: get_sender_label(event),
        DM_HISTORY_LIMIT,
        GROUP_HISTORY_LIMIT,
        HISTORY_SAVER.mark_dirty,
    )

    if triggered:
        effective_trigger = True
    elif not is_self:
        effective_trigger = chat_id in auto_respond or AUTO_RESPOND_GLOBAL
    else:
        effective_trigger = False

    if not effective_trigger:
        return

    if (
        COOLDOWN > 0
        and not is_self
        and core.check_cooldown(chat_id, now, COOLDOWN, last_chat_activity)
    ):
        logger.info("Кулдаун для чата %s, пропускаю", chat_id)
        return
    if is_self:
        core.check_cooldown(chat_id, now, COOLDOWN, last_chat_activity)

    logger.info("Запрос из чата %s от %s: %s", chat_id, sender_id, text[:100])

    prompt = TRIGGER_RE.sub("", text, count=1).strip()

    replied_text = await core.fetch_replied_text(message)

    if replied_text:
        if prompt:
            prompt = (
                f"Сообщение, на которое ответили:\n{replied_text}\n\nЗапрос: {prompt}"
            )
        else:
            prompt = replied_text

    if not prompt:
        return

    if len(prompt) > MAX_REQUEST_LEN:
        prompt = prompt[:MAX_REQUEST_LEN]

    model = model_for(chat_id)
    if is_private:
        hist, messages = await core.prepare_messages(
            STORE,
            chat_id,
            DM_HISTORY_LIMIT,
            GROUP_HISTORY_LIMIT,
            system_for,
        )
    else:
        label = await get_sender_label(event)
        user_content = f"{label}: {prompt}" if label else prompt
        await core.append_group_history(
            chat_id, user_content, chat_history, GROUP_HISTORY_LIMIT, ctx_lock
        )
        hist, messages = await core.prepare_messages(
            STORE,
            chat_id,
            DM_HISTORY_LIMIT,
            GROUP_HISTORY_LIMIT,
            system_for,
        )

    if is_self:
        prefix = f"{html.escape(text)}\n\n{model}:\n\n"
        self_edit_id = msg_id
    else:
        prefix = f"{model}:\n\n"
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
            render_response,
            edit_text,
            safe_reply,
            cast(Any, get_client().action(chat_id, "typing")),
            stream_with_tools,
            None,
            None,
            EDIT_INTERVAL,
        )

        async with ctx_lock:
            hist.append({"role": "assistant", "content": full_answer})
        HISTORY_SAVER.mark_dirty()

        if len(recent_reply_ids) > 5000:
            recent_reply_ids.clear()

    except (OpenAIError, RPCError, OSError, ValueError, TypeError):
        logger.exception("Ошибка генерации ответа")
        await safe_reply(
            event, "Ошибка при обращении к DanyAPI. / DanyAPI request error."
        )


async def disconnect_quietly(timeout=10):
    with contextlib.suppress(Exception):
        await HISTORY_SAVER.flush()
    if client is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(cast(Any, client.disconnect()), timeout=timeout)


async def start_userbot():
    if not API_ID or not API_HASH:
        logger.error("ENABLE_USERBOT=1, но API_ID/API_HASH не заданы в .env")
        return
    cli = get_client()
    cli.add_event_handler(handler, events.NewMessage(incoming=None))
    candidates = await proxies.get_proxy_candidates(limit=40)
    if not candidates:
        logger.warning("Нет прокси, пробую напрямую")
        candidates = [None]

    for idx, proxy in enumerate(candidates):
        if proxy:
            logger.info("Пробую прокси %d/%d: %s", idx + 1, len(candidates), proxy)
            cli.set_proxy(proxy)
        try:
            start_coro = cast(Any, cli.start())
            await asyncio.wait_for(start_coro, timeout=25)
            me = await cli.get_me()
            logger.info(
                "Бот запущен как %s (@%s)",
                getattr(me, "first_name", "?"),
                getattr(me, "username", "?"),
            )
            break
        except AuthKeyError as exc:
            logger.error("Сессия невалидна, подключение прервано: %s", exc)
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
                "Не удалось подключиться через %s: %s", proxy, type(exc).__name__
            )
            await disconnect_quietly()
            if proxy:
                proxies.mark_bad_proxy(proxies.telethon_to_item(proxy))
    else:
        logger.error("Не удалось подключиться ни через один прокси")
        return

    await cast(Any, cli.run_until_disconnected())
