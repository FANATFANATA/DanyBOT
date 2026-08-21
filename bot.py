import ast
import asyncio
import json
import logging
import math
import os
import re
import time
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

import proxies

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("danybot")

API_ID = int(os.getenv("API_ID", "2040") or "2040")
API_HASH = os.getenv("API_HASH", "") or "b18441a1ff607e10a989891a5462e627"
SESSION_NAME = os.getenv("SESSION_NAME", "session")

DANYAPI_URL = os.getenv("DANYAPI_URL", "http://127.0.0.1:8008/v1")
DANYAPI_MODEL = os.getenv("DANYAPI_MODEL", "deepseek-v4-flash")
DANYAPI_KEY = os.getenv("DANYAPI_KEY", "danyapi")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — DanyBOT, юзербот пользователя DanyaVoredom, работающий в Telegram. "
    "Отвечай максимально кратко и только по делу.",
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

EDIT_INTERVAL = float(os.getenv("EDIT_INTERVAL", "1"))
GROUP_HISTORY_LIMIT = int(os.getenv("GROUP_HISTORY_LIMIT", "40"))
DM_HISTORY_LIMIT = int(os.getenv("DM_HISTORY_LIMIT", "100"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))
MAX_REQUEST_LEN = int(os.getenv("MAX_REQUEST_LEN", "8000"))
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "8"))
AUTO_RESPOND_GLOBAL = os.getenv("AUTO_RESPOND", "0").strip() in (
    "1",
    "true",
    "yes",
)
COOLDOWN = float(os.getenv("COOLDOWN", "0"))
BOT_NAME = os.getenv("BOT_NAME", "DanyBOT").strip()
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", "").strip()

EXTRA_SYSTEM = ""
if SYSTEM_PROMPT_FILE:
    try:
        EXTRA_SYSTEM = Path(SYSTEM_PROMPT_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Не удалось прочитать SYSTEM_PROMPT_FILE: %s", exc)

STATE_FILE = Path(__file__).parent / "state.json"
HISTORY_FILE = Path(__file__).parent / "history.json"

MODELS = ["deepseek-v4-flash"]

model_overrides = {}
auto_respond = set()
ignored_chats = set()
ignored_users = set()

chat_history = {}
ctx_lock = asyncio.Lock()
recent_reply_ids = set()
last_chat_activity = {}
me_self = None


def make_session(name: str):
    value = name.strip()
    if value.startswith("1"):
        try:
            return StringSession(value)
        except (ValueError, TypeError):
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
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.warning("Не удалось загрузить state.json: %s", exc)


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
        logger.warning("Не удалось сохранить state.json: %s", exc)


def load_history():
    global chat_history
    if not HISTORY_FILE.exists():
        return
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        for key, value in data.items():
            try:
                chat_id = int(key)
            except (ValueError, TypeError):
                continue
            if chat_id > 0:
                limit = DM_HISTORY_LIMIT
            else:
                limit = GROUP_HISTORY_LIMIT
            chat_history[chat_id] = deque(value, maxlen=limit)
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.warning("Не удалось загрузить history.json: %s", exc)


def save_history():
    data = {str(k): list(v) for k, v in chat_history.items()}
    try:
        HISTORY_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except (OSError, TypeError) as exc:
        logger.warning("Не удалось сохранить history.json: %s", exc)


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


def system_for(chat_id):
    if EXTRA_SYSTEM:
        return f"{SYSTEM_PROMPT}\n\n{EXTRA_SYSTEM}"
    return SYSTEM_PROMPT


def get_history_for(chat_id) -> deque:
    return chat_history.setdefault(chat_id, deque(maxlen=GROUP_HISTORY_LIMIT))


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_chat_history",
            "description": "Получить последние сообщения текущего чата",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                    }
                },
                "required": ["limit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_chats",
            "description": "Получить список диалогов аккаунта (чаты, пользователи, каналы)",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to",
            "description": "Отправить сообщение в указанный чат или пользователю по id или username",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["chat", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chat_history_in",
            "description": "Получить последние сообщения указанного чата или пользователя",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["chat"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate",
            "description": "Безопасно вычислить математическое выражение",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chat_info",
            "description": "Информация о текущем чате",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_info",
            "description": "Информация о пользователе по username или id",
            "parameters": {
                "type": "object",
                "properties": {"handle": {"type": "string"}},
                "required": ["handle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_message",
            "description": "Отредактировать сообщение в текущем чате по id",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["message_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_message_by_id",
            "description": "Получить текст конкретного сообщения по id в текущем чате",
            "parameters": {
                "type": "object",
                "properties": {"message_id": {"type": "integer"}},
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_profile",
            "description": "Информация о своём аккаунте (юзерботе)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


SAFE_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "pi": math.pi,
    "e": math.e,
}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("Нечисловая константа")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        op = type(node.op)
        if op is ast.Add:
            return left + right
        if op is ast.Sub:
            return left - right
        if op is ast.Mult:
            return left * right
        if op is ast.Div:
            return left / right
        if op is ast.FloorDiv:
            return left // right
        if op is ast.Mod:
            return left % right
        if op is ast.Pow:
            return left**right
        raise ValueError("Недопустимый оператор")
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        op = type(node.op)
        if op is ast.UAdd:
            return +operand
        if op is ast.USub:
            return -operand
        raise ValueError("Недопустимый унарный оператор")
    if isinstance(node, ast.Name):
        if node.id in SAFE_FUNCS:
            return SAFE_FUNCS[node.id]
        raise ValueError("Недопустимое имя")
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in SAFE_FUNCS:
            args = [_eval_node(a) for a in node.args]
            return SAFE_FUNCS[node.func.id](*args)
        raise ValueError("Недопустимый вызов")
    raise ValueError("Недопустимая конструкция")


def safe_eval(expression: str) -> str:
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        return str(_eval_node(tree))
    except (
        SyntaxError,
        ValueError,
        ZeroDivisionError,
        OverflowError,
        TypeError,
        KeyError,
    ) as exc:
        return f"Ошибка вычисления: {exc}"


async def execute_tool(name: str, arguments: dict, chat_id):
    if name == "evaluate":
        return safe_eval(str(arguments.get("expression", "")))
    if name == "get_chat_history":
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))
        try:
            msgs = await client.get_messages(chat_id, limit=limit)
        except (RPCError, OSError, ValueError) as exc:
            return f"Ошибка получения истории: {exc}"
        if not msgs:
            return "История пуста."
        lines = []
        for m in reversed(list(msgs)):
            sender = getattr(m, "sender_id", None)
            text = m.message or ""
            lines.append(f"[{m.id}] {sender}: {text}")
        return "\n".join(lines)
    if name == "get_chat_info":
        try:
            entity = await client.get_entity(chat_id)
        except (RPCError, OSError, ValueError) as exc:
            return f"Ошибка получения чата: {exc}"
        return json.dumps(
            {
                "id": getattr(entity, "id", None),
                "title": getattr(entity, "title", None),
                "username": getattr(entity, "username", None),
                "members": getattr(entity, "participants_count", None),
            },
            ensure_ascii=False,
        )
    if name == "get_user_info":
        handle = str(arguments.get("handle", "")).strip()
        if not handle:
            return "Пустой handle."
        try:
            entity = await client.get_entity(handle)
        except (RPCError, OSError, ValueError) as exc:
            return f"Ошибка получения пользователя: {exc}"
        first = getattr(entity, "first_name", "") or ""
        last = getattr(entity, "last_name", "") or ""
        full = " ".join(x for x in [first, last] if x).strip()
        return json.dumps(
            {
                "id": getattr(entity, "id", None),
                "name": full,
                "username": getattr(entity, "username", None),
            },
            ensure_ascii=False,
        )
    if name == "edit_message":
        text = str(arguments.get("text", "")).strip()
        if not text:
            return "Пустой текст."
        try:
            message_id = int(arguments.get("message_id", 0))
            await client.edit_message(chat_id, message_id, text)
        except (RPCError, OSError, ValueError, TypeError) as exc:
            return f"Ошибка редактирования: {exc}"
        return "Сообщение отредактировано."
    if name == "list_chats":
        try:
            limit = int(arguments.get("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, 100))
        try:
            dialogs = await client.get_dialogs(limit=limit)
        except (RPCError, OSError, ValueError) as exc:
            return f"Ошибка получения диалогов: {exc}"
        lines = []
        for d in dialogs:
            eid = getattr(d.entity, "id", None)
            username = getattr(d.entity, "username", None)
            name = getattr(d, "name", "") or getattr(d, "title", "") or ""
            label = name if name else str(eid)
            if username:
                label += f" (@{username})"
            lines.append(f"{eid}: {label}")
        return "\n".join(lines) if lines else "Диалоги не найдены."
    if name == "send_message_to":
        chat = str(arguments.get("chat", "")).strip()
        text = str(arguments.get("text", "")).strip()
        if not chat or not text:
            return "Нужны chat и text."
        try:
            await client.send_message(chat, text)
            return "Сообщение отправлено."
        except (RPCError, OSError, ValueError) as exc:
            return f"Ошибка отправки: {exc}"
    if name == "get_chat_history_in":
        chat = str(arguments.get("chat", "")).strip()
        if not chat:
            return "Пустой chat."
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))
        try:
            msgs = await client.get_messages(chat, limit=limit)
        except (RPCError, OSError, ValueError) as exc:
            return f"Ошибка получения истории: {exc}"
        if not msgs:
            return "История пуста."
        lines = []
        for m in reversed(list(msgs)):
            sender = getattr(m, "sender_id", None)
            text = m.message or ""
            lines.append(f"[{m.id}] {sender}: {text}")
        return "\n".join(lines)
    if name == "get_message_by_id":
        try:
            message_id = int(arguments.get("message_id", 0))
            msgs = await client.get_messages(chat_id, ids=[message_id])
        except (RPCError, OSError, ValueError, TypeError) as exc:
            return f"Ошибка получения сообщения: {exc}"
        if not msgs:
            return "Сообщение не найдено."
        return msgs[0].message or ""
    if name == "get_profile":
        try:
            me = await client.get_me()
        except (RPCError, OSError, ValueError) as exc:
            return f"Ошибка получения профиля: {exc}"
        first = getattr(me, "first_name", "") or ""
        last = getattr(me, "last_name", "") or ""
        full = " ".join(x for x in [first, last] if x).strip()
        return json.dumps(
            {
                "id": getattr(me, "id", None),
                "name": full,
                "username": getattr(me, "username", None),
                "phone": getattr(me, "phone", None),
            },
            ensure_ascii=False,
        )
    return f"Неизвестная функция: {name}"


async def stream_with_tools(
    messages: list, model: str, chat_id, on_delta, on_reasoning
):
    working = [dict(m) for m in messages]
    rounds = 0
    content_parts = []
    while True:
        rounds += 1
        if rounds > MAX_TOOL_ROUNDS:
            return (
                "".join(content_parts)
                if content_parts
                else "Достигнут лимит циклов инструментов."
            )
        tool_calls = {}
        content_parts = []
        stream = await ai.chat.completions.create(
            model=model,
            messages=working,
            temperature=0.7,
            max_tokens=MAX_TOKENS,
            stream=True,
            tools=TOOLS,
        )
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
            result = await execute_tool(slot["name"], args, chat_id)
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
    try:
        sent = await event.reply(text)
        if sent:
            recent_reply_ids.add(sent.id)
            if len(recent_reply_ids) > 5000:
                recent_reply_ids.clear()
        return sent
    except FloodWaitError as e:
        await asyncio.sleep(min(e.seconds, 30))
        return await safe_reply(event, text)
    except (RPCError, OSError, ValueError, TypeError):
        return None


async def edit_text(chat_id, msg_id, text):
    try:
        await client.edit_message(chat_id, msg_id, text)
        return True
    except FloodWaitError as e:
        await asyncio.sleep(min(e.seconds, 30))
        return await edit_text(chat_id, msg_id, text)
    except (RPCError, OSError, ValueError, TypeError):
        return False


SUB_ALIASES = {
    "reset": ("reset", "сброс", "сбросить", "забыть", "clear", "забудь", "стоп"),
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
            return low[len(a) :].strip(), a
    return None, None


def handle_commands(text):
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

    if sub in SUB_ALIASES["reset"]:
        return ("reset",)
    if sub in SUB_ALIASES["models"]:
        return ("models",)
    if sub in SUB_ALIASES["model"]:
        return ("model", arg or None)
    if sub in SUB_ALIASES["history"]:
        return ("history",)
    if sub in SUB_ALIASES["help"]:
        return ("help",)
    if sub in SUB_ALIASES["ping"]:
        return ("ping",)
    if sub in SUB_ALIASES["ignore"]:
        return ("ignore",)
    if sub in SUB_ALIASES["unignore"]:
        return ("unignore",)

    return None


def models_text(chat_id) -> str:
    current = model_for(chat_id)
    parts = ["Доступные модели:"]
    for m in MODELS:
        marker = " (текущая)" if m == current else ""
        parts.append(f"• {m}{marker}")
    if current not in MODELS:
        parts.append(f"• {current} (текущая)")
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
    "Алиасы триггера: .danybot .danyapi .dany .db .gpt .ai .бот .д .б\n"
    "Алиасы команд: help/помощь, model/модель, models/модели, reset/сброс, "
    "clear/забудь, history/история, ping\n\n"
    "Команды:\n"
    ".danybot <текст> — вопрос\n"
    ".danybot reset — сбросить контекст\n"
    ".danybot model <id> — сменить модель\n"
    ".danybot models — список моделей\n"
    ".danybot model — показать текущую модель\n"
    ".danybot clear — очистить контекст\n"
    ".danybot history — размер контекста\n"
    ".danybot ping — статус\n"
    ".danybot ignore / unignore — заглушить/разглушить чат\n"
    ".danybot help — эта справка\n\n"
    "Авто-ответ: .danyauto on/off (.da .auto .авто)"
)


@client.on(events.NewMessage(incoming=None))
async def handler(event: events.NewMessage.Event):
    message = event.message
    if not message or not message.message:
        return

    text = message.message
    msg_id = message.id
    chat_id = event.chat_id
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
        await safe_reply(event, "Чат разглушен.")
        return

    if chat_id in ignored_chats:
        return
    if sender_id is not None and sender_id in ignored_users:
        return

    if command:
        cmd = command[0]
        if cmd == "reset":
            async with ctx_lock:
                limit = DM_HISTORY_LIMIT if is_private else GROUP_HISTORY_LIMIT
                chat_history[chat_id] = deque(maxlen=limit)
            save_history()
            await safe_reply(event, "Контекст сброшен.")
            return
        if cmd == "model":
            val = command[1]
            async with ctx_lock:
                if val:
                    model_overrides[chat_id] = val
                    text_out = f"Модель установлена: {val}"
                else:
                    text_out = f"Текущая модель: {model_for(chat_id)}"
            save_state()
            await safe_reply(event, text_out)
            return
        if cmd == "autorespond":
            val = command[1]
            async with ctx_lock:
                if val:
                    auto_respond.add(chat_id)
                    text_out = "Авто-ответ ВКЛ."
                else:
                    auto_respond.discard(chat_id)
                    text_out = "Авто-ответ ВЫКЛ."
            save_state()
            await safe_reply(event, text_out)
            return
        if cmd == "history":
            async with ctx_lock:
                hist = chat_history.get(chat_id, deque())
                n = len(hist)
                chars = sum(len(m["content"]) for m in hist)
            await safe_reply(event, f"Сообщений в контексте: {n}, символов: {chars}")
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
                f"Онлайн. Модель: {model_for(chat_id)}\n"
                f"Контекст: {len(chat_history.get(chat_id, deque()))} сообщений",
            )
            return
        if cmd == "ignore":
            async with ctx_lock:
                ignored_chats.add(chat_id)
            save_state()
            await safe_reply(event, "Чат заглушен. Анмут: .db unignore")
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
        prefix = f"{text}\n\n{model}:\n\n"
        edit_id = msg_id
    else:
        prefix = f"{model}:\n\n"
        edit_id = None

    try:
        full_answer = ""
        last_edit = 0.0
        reasoning_parts = []

        def render():
            out = prefix
            if reasoning_parts:
                out += f"💭 {''.join(reasoning_parts)}\n\n"
            out += full_answer
            return out

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

        async with client.action(chat_id, "typing"):
            if not is_self:
                placeholder = await event.reply("…")
                if placeholder:
                    edit_id = placeholder.id
                    recent_reply_ids.add(edit_id)

            result = await stream_with_tools(
                messages, model, chat_id, on_delta, on_reasoning
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
        await safe_reply(event, "Ошибка при обращении к DanyAPI.")


async def main():
    global me_self

    load_state()
    load_history()
    await refresh_models()

    candidates = await proxies.get_proxy_candidates(limit=40)
    if not candidates:
        logger.warning("Нет прокси, пробую напрямую")
        candidates = [None]

    for idx, proxy in enumerate(candidates):
        if proxy:
            logger.info("Пробую прокси %d/%d: %s", idx + 1, len(candidates), proxy)
            client.set_proxy(proxy)
        try:
            await asyncio.wait_for(client.start(), timeout=25)
            me_self = await client.get_me()
            logger.info(
                "Бот запущен как %s (@%s)", me_self.first_name, me_self.username
            )
            break
        except (
            OSError,
            RPCError,
            ConnectionError,
            asyncio.TimeoutError,
            FloodWaitError,
        ) as exc:
            logger.warning(
                "Не удалось подключиться через %s: %s", proxy, type(exc).__name__
            )
            if proxy:
                try:
                    await client.disconnect()
                except (RPCError, OSError, ConnectionError) as exc:
                    logger.warning("Ошибка отключения от прокси: %s", exc)
    else:
        logger.error("Не удалось подключиться ни через один прокси")
        return

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
