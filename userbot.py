import asyncio
import contextlib
import html
import json
import logging
import os
import re
import struct
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError
from telethon import TelegramClient, events
from telethon.errors import AuthKeyError, FloodWaitError, RPCError
from telethon.sessions import StringSession

import proxies
import tools as tools_module

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("danybot")


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name, "")
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name, "")
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name, "")
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no")


API_ID = _env_int("API_ID", 2040)
API_HASH = os.getenv("API_HASH", "") or "b18441a1ff607e10a989891a5462e627"
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

TRIGGER_ALIASES = (
    ".danybot",
    ".danyapi",
    ".dany",
    ".db",
    ".gpt",
    ".ai",
    ".bot",
    ".бот",
    ".д",
    ".d",
    ".б",
    ".данибот",
    ".даниапи",
    ".дани",
)
AUTO_ALIASES = (".danyauto", ".da", ".auto", ".авто", ".даниавто")


def _alias_pattern(aliases):
    return r"(?:" + "|".join(re.escape(a) for a in aliases) + r")"


def _build_trigger_re():
    return re.compile(
        r"(?<![a-zа-я0-9])" + _alias_pattern(TRIGGER_ALIASES) + r"(?![a-zа-я0-9])",
        re.IGNORECASE,
    )


TRIGGER_RE = _build_trigger_re()

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

REPLY_ATTEMPTS = 3

EXTRA_SYSTEM = ""
if SYSTEM_PROMPT_FILE:
    try:
        EXTRA_SYSTEM = Path(SYSTEM_PROMPT_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Не удалось прочитать SYSTEM_PROMPT_FILE: %s", exc)

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
last_chat_activity: dict[int, float] = {}
START_TIME = time.monotonic()


def make_session(name: str):
    value = name.strip()
    if value.startswith("1"):
        try:
            return StringSession(value)
        except (ValueError, TypeError, struct.error):
            return name
    return name


client = TelegramClient(
    make_session(SESSION_NAME),
    API_ID,
    API_HASH,
    connection_retries=2,
    request_retries=1,
    retry_delay=0,
    timeout=10,
)
ai = AsyncOpenAI(base_url=DANYAPI_URL, api_key=DANYAPI_KEY)


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
        logger.warning("Не удалось загрузить state_userbot.json: %s", exc)


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
        logger.warning("Не удалось сохранить state_userbot.json: %s", exc)


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
            limit = DM_HISTORY_LIMIT if chat_id > 0 else GROUP_HISTORY_LIMIT
            chat_history[chat_id] = deque(value, maxlen=limit)
    except (
        json.JSONDecodeError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        logger.warning("Не удалось загрузить history_userbot.json: %s", exc)


def save_history():
    data = {str(k): list(v) for k, v in chat_history.items()}
    try:
        HISTORY_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except (OSError, TypeError) as exc:
        logger.warning("Не удалось сохранить history_userbot.json: %s", exc)


def strip_role_tag(text: str) -> str:
    low = text.strip().lower()
    if low.startswith(f"{BOT_NAME.lower()}:"):
        return text.split(":", 1)[1].strip()
    m = re.match(r"^\[(user|assistant|system)\]\s*", text, re.IGNORECASE)
    if m:
        return text[m.end() :]
    return text


def model_for(chat_id):
    return model_overrides.get(chat_id, DANYAPI_MODEL)


def system_for(chat_id, mode="userbot"):
    base = SYSTEM_PROMPT_BOT if mode == "bot" else SYSTEM_PROMPT
    if EXTRA_SYSTEM:
        return f"{base}\n\n{EXTRA_SYSTEM}"
    return base


def get_history_for(chat_id) -> deque:
    return chat_history.setdefault(chat_id, deque(maxlen=GROUP_HISTORY_LIMIT))


TOOLS = tools_module.TOOLS
TOOLS_BOT = tools_module.TOOLS
safe_eval = tools_module.safe_eval
render_response = tools_module.render_response


def _bot_stats(chat_id):
    import sys as _sys

    uptime = time.monotonic() - START_TIME
    return {
        "uptime_seconds": round(uptime, 1),
        "python": _sys.version.split()[0],
        "context_messages": len(chat_history.get(chat_id, deque())),
        "model": model_for(chat_id),
        "modes": {"userbot": ENABLE_USERBOT, "bot": ENABLE_BOT},
    }


async def execute_tool(name: str, arguments: dict, chat_id, client=None):
    if client is None:
        client = globals()["client"]
    return await tools_module.execute_tool(name, arguments, chat_id, client, _bot_stats)


async def stream_with_tools(
    messages: list,
    model: str,
    chat_id,
    on_delta,
    on_reasoning,
    on_tool=None,
    client_override=None,
    tools=None,
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
        for _idx, slot in sorted(tool_calls.items()):
            try:
                args = json.loads(slot["arguments"] or "{}")
            except (json.JSONDecodeError, ValueError):
                args = {}
            if client_override is None:
                result = await execute_tool(slot["name"], args, chat_id)
            else:
                result = await execute_tool(
                    slot["name"], args, chat_id, client_override
                )
            if on_tool is not None:
                await on_tool(slot["name"])
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
    for _attempt in range(REPLY_ATTEMPTS):
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
    for _attempt in range(REPLY_ATTEMPTS):
        try:
            await client.edit_message(chat_id, msg_id, text, parse_mode="html")
            return True
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 30))
            continue
        except (RPCError, OSError, ValueError, TypeError):
            return False
    return False


SUB_ALIASES = {
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
    "help": ("help", "помощь", "хелп", "справка", "?"),
    "ping": ("ping", "пинг", "check"),
    "ignore": ("ignore", "игнор", "мут", "заглушить"),
    "unignore": ("unignore", "анмут", "размут", "включить"),
}

AUTO_ON_WORDS = ("on", "вкл", "включить", "1", "true", "yes", "да")
AUTO_OFF_WORDS = ("off", "выкл", "выключить", "0", "false", "no", "нет")


def _strip_alias_prefix(low):
    all_aliases = sorted(
        list(TRIGGER_ALIASES) + list(AUTO_ALIASES), key=len, reverse=True
    )
    for a in all_aliases:
        if low.startswith(a):
            tail = low[len(a) :]
            if tail and not tail[0].isspace():
                continue
            return tail.strip(), a
    return None, None


def handle_commands(text) -> tuple[str, Any] | None:
    low = text.strip()
    lowlower = low.lower()
    if not low.startswith("."):
        return None

    rest, alias = _strip_alias_prefix(lowlower)
    if alias is None or not rest:
        return None

    m = re.match(r"^(\S+)(?:\s+(.*))?$", rest, re.DOTALL)
    if not m:
        return None
    sub = m.group(1)
    arg = (m.group(2) or "").strip()

    if alias in AUTO_ALIASES:
        if sub in AUTO_ON_WORDS:
            return ("autorespond", True)
        if sub in AUTO_OFF_WORDS:
            return ("autorespond", False)

    if sub in SUB_ALIASES["clear"]:
        return ("clear", None)
    if sub in SUB_ALIASES["models"]:
        return ("models", None)
    if sub in SUB_ALIASES["model"]:
        return ("model", arg or None)
    if sub in SUB_ALIASES["history"]:
        return ("history", None)
    if sub in SUB_ALIASES["help"]:
        return ("help", None)
    if sub in SUB_ALIASES["ping"]:
        return ("ping", None)
    if sub in SUB_ALIASES["ignore"]:
        return ("ignore", None)
    if sub in SUB_ALIASES["unignore"]:
        return ("unignore", None)

    return None


def models_text(chat_id) -> str:
    current = model_for(chat_id)
    parts = ["Доступные модели / Available models:"]
    for m in MODELS:
        marker = " (текущая) / current" if m == current else ""
        parts.append(f"• {m}{marker}")
    if current not in MODELS:
        parts.append(f"• {current} (текущая) / current")
    return "\n".join(parts)


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


@client.on(events.NewMessage(incoming=None))
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
        cmd = command[0]
        if cmd == "clear":
            async with ctx_lock:
                limit = DM_HISTORY_LIMIT if is_private else GROUP_HISTORY_LIMIT
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
                    text_out = f"Текущая модель / Current model: {model_for(chat_id)}"
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
            await safe_reply(event, HELP_TEXT)
            return
        if cmd == "models":
            await safe_reply(event, models_text(chat_id))
            return
        if cmd == "ping":
            await safe_reply(
                event,
                f"Онлайн / Online. Модель / Model: {model_for(chat_id)}\n"
                f"Контекст / Context: "
                f"{len(chat_history.get(chat_id, deque()))} сообщений / messages",
            )
            return
        if cmd == "ignore":
            async with ctx_lock:
                ignored_chats.add(chat_id)
            save_state()
            await safe_reply(
                event,
                "Чат заглушен / Chat muted. Размут / Unmute: .db unignore",
            )
            return

    if is_private and text.strip() != "…":
        clean_text = (
            TRIGGER_RE.sub("", text, count=1).strip() if triggered else text.strip()
        )
        if is_self:
            role = "user" if triggered else "assistant"
            content = strip_role_tag(clean_text or text.strip())
        else:
            role = "user"
            label = await get_sender_label(event)
            content = f"{label}: {clean_text or text.strip()}"
        async with ctx_lock:
            hist = chat_history.setdefault(chat_id, deque(maxlen=DM_HISTORY_LIMIT))
            hist.append({"role": role, "content": content})
            save_history()

    if triggered:
        effective_trigger = True
    elif not is_self:
        effective_trigger = chat_id in auto_respond or AUTO_RESPOND_GLOBAL
    else:
        effective_trigger = False

    if not effective_trigger:
        return

    if COOLDOWN > 0 and not is_self:
        last = last_chat_activity.get(chat_id, 0)
        if (now - last) < COOLDOWN:
            logger.info("Кулдаун для чата %s, пропускаю", chat_id)
            return

    last_chat_activity[chat_id] = time.monotonic()
    if len(last_chat_activity) > 10000:
        cutoff = time.monotonic() - 3600
        for k in list(last_chat_activity):
            if last_chat_activity[k] < cutoff:
                del last_chat_activity[k]

    logger.info("Запрос из чата %s от %s: %s", chat_id, sender_id, text[:100])

    prompt = TRIGGER_RE.sub("", text, count=1).strip()

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

    if len(prompt) > MAX_REQUEST_LEN:
        prompt = prompt[:MAX_REQUEST_LEN]

    if is_private:
        async with ctx_lock:
            hist = chat_history.setdefault(chat_id, deque(maxlen=DM_HISTORY_LIMIT))
            sysp = system_for(chat_id)
            model = model_for(chat_id)
            messages = [{"role": "system", "content": sysp}, *list(hist)]
    else:
        label = await get_sender_label(event)
        user_content = f"{label}: {prompt}" if label else prompt
        async with ctx_lock:
            hist = get_history_for(chat_id)
            hist.append({"role": "user", "content": user_content})
            sysp = system_for(chat_id)
            model = model_for(chat_id)
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
            return render_response(prefix, reasoning_parts, tool_parts, full_answer)

        async def on_delta(part):
            nonlocal full_answer, last_edit
            full_answer += part
            now_m = time.monotonic()
            if edit_id is not None and (now_m - last_edit) >= EDIT_INTERVAL:
                await edit_text(chat_id, edit_id, render())
                last_edit = now_m

        async def on_reasoning(part):
            nonlocal last_edit
            reasoning_parts.append(part)
            now_m = time.monotonic()
            if edit_id is not None and (now_m - last_edit) >= EDIT_INTERVAL:
                await edit_text(chat_id, edit_id, render())
                last_edit = now_m

        async def on_tool(name):
            nonlocal last_edit
            tool_parts.append(name)
            now_m = time.monotonic()
            if edit_id is not None and (now_m - last_edit) >= EDIT_INTERVAL:
                await edit_text(chat_id, edit_id, render())
                last_edit = now_m

        async with cast(Any, client.action(chat_id, "typing")):
            if not is_self:
                placeholder = await event.reply("…")
                if placeholder:
                    edit_id = placeholder.id
                    recent_reply_ids.add(edit_id)

            result = await stream_with_tools(
                messages, model, chat_id, on_delta, on_reasoning, on_tool
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

    except (OpenAIError, RPCError, OSError, ValueError, TypeError):
        logger.exception("Ошибка генерации ответа")
        await safe_reply(
            event, "Ошибка при обращении к DanyAPI. / DanyAPI request error."
        )


async def disconnect_quietly(timeout=10):
    with contextlib.suppress(Exception):
        await asyncio.wait_for(cast(Any, client.disconnect()), timeout=timeout)


async def start_userbot():
    candidates = await proxies.get_proxy_candidates(limit=40)
    if not candidates:
        logger.warning("Нет прокси, пробую напрямую")
        candidates = [None]

    for idx, proxy in enumerate(candidates):
        if proxy:
            logger.info("Пробую прокси %d/%d: %s", idx + 1, len(candidates), proxy)
            client.set_proxy(proxy)
        try:
            start_coro = cast(Any, client.start())
            await asyncio.wait_for(start_coro, timeout=25)
            me = await client.get_me()
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

    await cast(Any, client.run_until_disconnected())
