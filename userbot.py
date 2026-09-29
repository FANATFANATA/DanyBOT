import asyncio
import contextlib
import json
import logging
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
API_HASH = _env_str("API_HASH", "")
SESSION_NAME = _env_str("SESSION_NAME", "session")

DANYAPI_URL = _env_str("DANYAPI_URL", "http://127.0.0.1:8008/v1")
DANYAPI_MODEL = _env_str("DANYAPI_MODEL", "deepseek-v4.1-flash")
DANYAPI_KEY = _env_str("DANYAPI_KEY", "danyapi")
SYSTEM_PROMPT = _env_str(
    "SYSTEM_PROMPT",
    "Ты - DanyBOT, юзербот пользователя DanyaVoredom, работающий в Telegram. "
    "Отвечай максимально кратко и только по делу.",
)
SYSTEM_PROMPT_BOT = _env_str(
    "SYSTEM_PROMPT_BOT",
    "Ты - DanyBOT, Telegram-бот. Отвечай максимально кратко и только по делу. "
    "Всегда отвечай на том языке, на котором написано последнее сообщение "
    "пользователя.",
)
TOOL_VERIFY_PROMPT = _env_str(
    "TOOL_VERIFY_PROMPT",
    "Ты - система безопасности Telegram-бота. Тебе показывают вызов инструмента "
    "с аргументами. Оцени, безопасно ли его выполнять: не приведёт ли он к удалению "
    "или порче данных, утечке приватной информации, выполнению опасных shell-команд, "
    "рассылке сообщений, действиям против владельца аккаунта. "
    "Ответь строго одним словом: ALLOW или DENY.",
)
TOOL_VERIFY_MODEL = _env_str("TOOL_VERIFY_MODEL", "")
VERIFY_TOKENS_SHELL = _env_int("VERIFY_TOKENS_SHELL", 1024)
VERIFY_TOKENS_TOOL = _env_int("VERIFY_TOKENS_TOOL", 512)
RUN_SHELL_MODEL = _env_str("RUN_SHELL_MODEL", "")
RUN_SHELL_VERIFY_PROMPT = _env_str(
    "RUN_SHELL_VERIFY_PROMPT",
    "Ты - система безопасности Telegram-бота. Тебе показывают вызов инструмента "
    "run_shell с shell-командой. Сначала рассуждай по шагам: что именно выполнит "
    "команда, какие файлы/данные затронет, есть ли удаление, перезапись, эксфильтрация "
    "секретов, обращение к сети, повышение прав, действия против владельца аккаунта. "
    "Затем на последней строке ответь строго одним словом: ALLOW или DENY.",
)
SANITIZE_ENABLED = _env_bool("SANITIZE_ENABLED", True)
SANITIZE_MODEL = _env_str("SANITIZE_MODEL", "")
SANITIZE_PROMPT = _env_str(
    "SANITIZE_PROMPT",
    "Ты - фильтр секретов. Тебе дают вывод shell-команды. Сначала рассуждай по шагам, "
    "затем верни ТОЛЬКО очищенный текст: удали или замени на [REDACTED] приватные "
    "данные - значения из .env и любых конфигов, токены, API-ключи, пароли, хеши, "
    "cookie, приватные ключи, строки сессий, URL с credentials. Остальной текст "
    "сохрани дословно. Не добавляй пояснений, верни только очищенный вывод.",
)

TRIGGER_RE = core.TRIGGER_RE

EDIT_INTERVAL = max(0.2, _env_float("EDIT_INTERVAL", 1.0))
GROUP_HISTORY_LIMIT = max(2, _env_int("GROUP_HISTORY_LIMIT", 40))
DM_HISTORY_LIMIT = max(2, _env_int("DM_HISTORY_LIMIT", 100))
LIVE_HISTORY_LIMIT = max(2, _env_int("LIVE_HISTORY_LIMIT", 50))
MAX_TOKENS = max(64, _env_int("MAX_TOKENS", 4096))
MAX_REQUEST_LEN = max(100, _env_int("MAX_REQUEST_LEN", 8000))
TOOL_CONTEXT_MESSAGES = max(8, _env_int("TOOL_CONTEXT_MESSAGES", 60))
REQUEST_TIMEOUT = max(10.0, _env_float("REQUEST_TIMEOUT", 120.0))
COOLDOWN = max(0.0, _env_float("COOLDOWN", 0.0))
BOT_NAME = _env_str("BOT_NAME", "DanyBOT")
SYSTEM_PROMPT_FILE = _env_str("SYSTEM_PROMPT_FILE", "")

CODER_SYSTEM_PROMPT = _env_str(
    "CODER_SYSTEM_PROMPT",
    "Ты - DanyBOT в режиме кодера и работаешь как автономный агент. "
    "Доступны инструменты read_file, write_file, edit_file, list_dir, "
    "search_files, execute_script, run_shell, web_search, fetch_url, get_time, "
    "memory_remember, memory_recall, memory_forget, memory_list, "
    "save_skill, load_skill, list_skills, delete_skill, run_subagent. "
    "Правила цикла: задача считается выполненной только когда ты реально всё "
    "сделал и проверил результат инструментами; не заканчивай ход, пока задача "
    "не выполнена; никогда не пиши 'сейчас сделаю', 'сейчас посмотрю', 'давай "
    "проверю' - вместо этого сразу вызывай нужный инструмент; после каждого "
    "результата анализируй его и вызывай следующий инструмент, пока не дойдёшь "
    "до конца; при ошибке инструмента исправь причину и повтори, но если "
    "ошибка повторяется - меняй подход, а не повторяй тот же вызов; отказ "
    "проверки безопасности означает, что вызов запрещён, - не повторяй его, "
    "разберись с причиной; не выдумывай содержимое файлов и вывод команд, всегда "
    "получай их инструментами; текстовый ответ без вызова инструмента означает "
    "завершение задачи, поэтому пиши его только когда всё готово; перед финальным "
    "ответом обязательно запусти проверку через execute_script или run_shell и "
    "покажи её вывод, а если проверка невозможна - прямо скажи почему; в финале "
    "дай краткий отчёт: что сделано, какие файлы изменены, результат проверки. "
    "Отвечай на языке последнего сообщения.",
)

ENABLE_USERBOT = _env_bool("ENABLE_USERBOT", True)
ENABLE_BOT = _env_bool("ENABLE_BOT", False)
BOT_TOKEN = _env_str("BOT_TOKEN", "")


def _env_pos_int(name: str) -> int | None:
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


def _env_id_set(name: str) -> set[int]:
    raw = _env_str(name, "")
    ids = set()
    for part in raw.replace(";", ",").split(","):
        chunk = part.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return ids


OWNER_IDS = _env_id_set("OWNER_IDS")

REPLY_ATTEMPTS = 3

HANDLER_ERRORS: tuple[type[BaseException], ...] = (
    *core.STREAM_ERRORS,
    RPCError,
    OpenAIError,
)

SANITIZED_TOOLS = ("run_shell", "execute_script", "run_subagent")


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

MODELS: list[str] = [DANYAPI_MODEL]

model_overrides: dict[int, str] = {}
coder_chats: set[int] = set()
reasoning_hidden: set[int] = set()
tools_hidden: set[int] = set()

chat_history: dict[int, deque] = {}
ctx_lock = asyncio.Lock()
recent_reply_ids: set[tuple[int, int]] = set()
seen_msg_keys: set[tuple[int, int]] = set()
last_chat_activity: dict[int, float] = {}
START_TIME = time.monotonic()

STORE: core.ModeStore = core.ModeStore(globals())
SESSIONS = core.SessionRegistry()
TASKS = core.TaskJournal(lambda: save_state())


def make_session(name: str):
    value = name.strip()
    if value.startswith("1"):
        try:
            return StringSession(value)
        except (ValueError, TypeError, struct.error):
            return name
    return name


client = None


def get_client(dc=None):
    global client
    if client is None:
        session = make_session(SESSION_NAME)
        options: dict[str, Any] = {
            "connection_retries": 2,
            "request_retries": 1,
            "retry_delay": 0,
            "timeout": 10,
        }
        client = TelegramClient(session, API_ID, API_HASH, **options)
        pin_datacenter(client, dc)
    return client


def pin_datacenter(cli, dc) -> None:
    address = core.dc_address(dc)
    if not address:
        return
    session = cli.session
    if getattr(session, "auth_key", None) is not None and session.dc_id:
        logger.info(
            "Сессия уже авторизована на ДЦ %s, оставляю адрес сессии", session.dc_id
        )
        return
    session.set_dc(int(dc), address, core.DC_PORT)


ai = AsyncOpenAI(
    base_url=DANYAPI_URL,
    api_key=DANYAPI_KEY,
    timeout=REQUEST_TIMEOUT,
    max_retries=1,
)


def load_state():
    core.load_state_into(STORE, STATE_FILE, logger, journal=TASKS)


def save_state():
    core.save_state_from(STORE, STATE_FILE, logger, journal=TASKS)


def load_history():
    core.load_history_into(
        STORE, HISTORY_FILE, DM_HISTORY_LIMIT, GROUP_HISTORY_LIMIT, logger
    )


def save_history():
    core.save_history_from(STORE, HISTORY_FILE, logger)


HISTORY_SAVER = core.AsyncSaver(save_history, delay=0.5, logger=logger)


def model_for(chat_id):
    return model_overrides.get(chat_id, DANYAPI_MODEL)


CONTRACT_ENABLED = _env_bool("CONTRACT_ENABLED", True)
CONTRACT_FILES = ("capabilities.server.md", "contract.md")
CONTRACT_ALIASES = {"capabilities.server.md": ("capabilities.md",)}
CONTRACT_DIR_CANDIDATES = (
    Path("/root/cdn"),
    Path(__file__).resolve().parent.parent / "cdn",
    Path(__file__).resolve().parent / "cdn",
)

_contract_cache: dict[str, Any] = {"key": None, "text": ""}


def _resolve_contract_dir():
    raw = _env_str("CONTRACT_DIR", "")
    if raw:
        return Path(raw)
    for candidate in CONTRACT_DIR_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return CONTRACT_DIR_CANDIDATES[0]


CONTRACT_DIR = _resolve_contract_dir()


def _contract_file(name):
    path = CONTRACT_DIR / name
    if path.is_file():
        return path
    for alias in CONTRACT_ALIASES.get(name, ()):
        alias_path = CONTRACT_DIR / alias
        if alias_path.is_file():
            return alias_path
    return None


def _contract_status(name):
    path = _contract_file(name)
    if path is None:
        return f"{name} (нет)"
    return f"{path.name} (ok)"


def _contract_signature():
    signature = []
    for name in CONTRACT_FILES:
        path = _contract_file(name)
        if path is None:
            signature.append((name, "", 0, 0))
            continue
        try:
            stat = path.stat()
        except OSError:
            signature.append((name, path.name, 0, 0))
            continue
        signature.append((name, path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def load_contract():
    if not CONTRACT_ENABLED:
        return ""
    signature = _contract_signature()
    if signature == _contract_cache["key"]:
        return _contract_cache["text"]
    blocks = []
    for name in CONTRACT_FILES:
        path = _contract_file(name)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Не удалось прочитать %s: %s", path, exc)
            continue
        if text:
            blocks.append(text)
    combined = "\n\n".join(blocks)
    _contract_cache["key"] = signature
    _contract_cache["text"] = combined
    return combined


def system_for(chat_id, mode="userbot", with_contract=False):
    if mode == "coder":
        base = CODER_SYSTEM_PROMPT
    elif mode == "bot":
        base = SYSTEM_PROMPT_BOT
    else:
        base = SYSTEM_PROMPT
    parts = [base]
    if EXTRA_SYSTEM:
        parts.append(EXTRA_SYSTEM)
    if with_contract and mode == "coder":
        contract = load_contract()
        if contract:
            parts.append(contract)
    return "\n\n".join(parts)


def settings_report(chat_id):
    current = model_for(chat_id)
    ctx_len = len(chat_history.get(chat_id, deque()))
    limit = DM_HISTORY_LIMIT if chat_id > 0 else GROUP_HISTORY_LIMIT
    reasoning_state = "скрыто" if chat_id in reasoning_hidden else "видно"
    tools_state = "скрыто" if chat_id in tools_hidden else "видно"
    contract = load_contract()
    task_state = core.task_state_label(TASKS.get(chat_id))
    return (
        "Настройки / Settings\n"
        f"Модель / Model: {current}\n"
        f"Рассуждения / Reasoning: {reasoning_state}\n"
        f"Инструменты / Tools: {tools_state}\n"
        f"Контекст / Context: {ctx_len}/{limit}\n"
        f"Задача / Task: {task_state}\n"
        f"Контракт / Contract: {'включён' if contract else 'выключен'} "
        f"({len(contract)} символов)"
    )


def system_prompt_report(chat_id, mode="userbot", limit=3000, with_contract=False):
    text = system_for(chat_id, mode=mode, with_contract=with_contract)
    contract = load_contract()
    files = ", ".join(_contract_status(name) for name in CONTRACT_FILES)
    head = (
        f"Режим / Mode: {mode}\n"
        f"Длина / Length: {len(text)} символов\n"
        f"Контракт / Contract: "
        f"{'включён' if contract else 'выключен'} ({len(contract)} символов)\n"
        f"Источник / Source: {CONTRACT_DIR} - {files}\n"
    )
    if len(text) <= limit:
        return f"{head}\n{text}"
    return f"{head}\n{text[:limit]}\n… (обрезано / truncated)"


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


async def execute_tool(
    name: str,
    arguments: dict,
    chat_id,
    tool_client=None,
    stats=None,
    unrestricted=False,
    allowed=None,
):
    if tool_client is None:
        tool_client = client
    return await tools_module.execute_tool(
        name,
        arguments,
        chat_id,
        tool_client,
        stats or _bot_stats,
        unrestricted,
        allowed,
    )


def _last_decision(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        matches = re.findall(r"\b(ALLOW|DENY)\b", stripped.upper())
        if matches:
            return matches[-1]
    return ""


async def verify_tool_call(
    name: str, arguments: dict, model: str, unrestricted: bool = False
) -> bool:
    if unrestricted:
        return True
    payload = json.dumps({"tool": name, "arguments": arguments}, ensure_ascii=False)
    if name == "run_shell":
        prompt = RUN_SHELL_VERIFY_PROMPT
        use_model = RUN_SHELL_MODEL or model
        max_tokens = VERIFY_TOKENS_SHELL
    else:
        prompt = TOOL_VERIFY_PROMPT
        use_model = model
        max_tokens = VERIFY_TOKENS_TOOL
    try:
        resp = await asyncio.wait_for(
            ai.chat.completions.create(
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
            ),
            timeout=REQUEST_TIMEOUT,
        )
    except (OpenAIError, OSError, ValueError, TypeError, asyncio.TimeoutError) as exc:
        logger.warning("Верификация %s недоступна: %r", name, exc)
        return False
    try:
        message = resp.choices[0].message
    except (AttributeError, IndexError, TypeError) as exc:
        logger.warning("Верификация %s: пустой ответ: %r", name, exc)
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


async def sanitize_tool_output(
    output: str, model: str, unrestricted: bool = False
) -> str:
    if unrestricted or not SANITIZE_ENABLED or not output:
        return output
    use_model = SANITIZE_MODEL or model
    try:
        resp = await asyncio.wait_for(
            ai.chat.completions.create(
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
            ),
            timeout=REQUEST_TIMEOUT,
        )
    except (OpenAIError, OSError, ValueError, TypeError, asyncio.TimeoutError) as exc:
        logger.warning("Санитайзер недоступен: %r", exc)
        return "[вывод скрыт: ошибка санитайзера]"
    try:
        message = resp.choices[0].message
    except (AttributeError, IndexError, TypeError) as exc:
        logger.warning("Санитайзер: пустой ответ: %r", exc)
        return "[вывод скрыт: пустой ответ санитайзера]"
    raw_content = getattr(message, "content", None)
    if isinstance(raw_content, list):
        content = "".join(str(x) for x in raw_content).strip()
    else:
        content = (raw_content or "").strip()
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
    sanitize_tools=True,
    unrestricted=False,
    stats=None,
    on_progress=None,
):
    working: list[dict[str, Any]] = [dict(m) for m in messages]
    rounds = 0
    all_parts: list[str] = []
    allowed = tools_module.tool_names_of(tools if tools is not None else TOOLS)
    calls_made = 0
    while True:
        rounds += 1
        working = core.trim_tool_history(working, TOOL_CONTEXT_MESSAGES)
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts = []
        raw_stream = await asyncio.wait_for(
            ai.chat.completions.create(
                model=model,
                messages=cast(Any, working),
                temperature=0.7,
                max_tokens=MAX_TOKENS,
                stream=True,
                tools=cast(Any, tools if tools is not None else TOOLS),
            ),
            timeout=REQUEST_TIMEOUT,
        )
        stream = cast(Any, raw_stream)
        stream_iter = stream.__aiter__()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        stream_iter.__anext__(), timeout=REQUEST_TIMEOUT
                    )
                except StopAsyncIteration:
                    break
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
                    all_parts.append(delta.content)
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
        finally:
            with contextlib.suppress(Exception):
                await stream.close()
        if not tool_calls:
            _report_progress(on_progress, rounds, calls_made)
            return "".join(all_parts)
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
        calls_made += len(slots)
        _report_progress(on_progress, rounds, calls_made)

        async def run_slot(slot):
            try:
                args = json.loads(slot["arguments"] or "{}")
            except (json.JSONDecodeError, ValueError) as exc:
                snippet = str(slot["arguments"])[:200]
                return f"Некорректный JSON аргументов: {exc}. Args: {snippet}"
            if not isinstance(args, dict):
                return (
                    "Аргументы должны быть объектом JSON, получено: "
                    f"{type(args).__name__}"
                )
            verify_model = TOOL_VERIFY_MODEL or model
            if (
                verify_tools
                and not unrestricted
                and not await verify_tool_call(slot["name"], args, verify_model)
            ):
                return "Вызов отклонён проверкой безопасности. Не повторяй его."
            if on_tool is not None:
                await on_tool(slot["name"])
            result = await execute_tool(
                slot["name"],
                args,
                chat_id,
                client_override,
                stats,
                unrestricted,
                allowed,
            )
            if slot["name"] in SANITIZED_TOOLS and sanitize_tools:
                return await sanitize_tool_output(result, model)
            return result

        results: list[Any] = list(
            await asyncio.gather(*(run_slot(s) for s in slots), return_exceptions=True)
        )
        for slot, outcome in zip(slots, results, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                logger.warning("Инструмент %s упал: %r", slot["name"], outcome)
                content = f"Ошибка инструмента {slot['name']}: {outcome}"
            else:
                content = outcome
            working.append(
                {
                    "role": "tool",
                    "tool_call_id": slot["id"],
                    "content": content,
                }
            )


def _report_progress(on_progress, rounds, calls_made, reason="") -> None:
    if on_progress is None:
        return
    with contextlib.suppress(Exception):
        on_progress(rounds, calls_made, reason)


async def _get_sender(event):
    try:
        return await event.get_sender()
    except (RPCError, OSError, ValueError, TypeError, AttributeError) as exc:
        logger.debug("Не удалось получить отправителя: %r", exc)
        return None


async def _sender_is_bot(event) -> bool:
    return bool(getattr(await _get_sender(event), "bot", False))


async def get_sender_label(event):
    sender = await _get_sender(event)
    if not sender:
        return str(event.sender_id)
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    username = getattr(sender, "username", None)
    full = " ".join(x for x in [first, last] if x).strip()
    if username:
        return f"{full} (@{username})" if full else f"@{username}"
    return full or str(event.sender_id)


async def fetch_live_messages(chat_id, limit, client=None):
    cli = client if client is not None else get_client()
    try:
        msgs = cast(Any, await cli.get_messages(chat_id, limit=limit))
    except (RPCError, OSError, ValueError):
        return []
    out = []
    for m in msgs:
        text = m.message or ""
        if not text.strip():
            continue
        role = "assistant" if getattr(m, "out", False) else "user"
        out.append({"role": role, "content": text})
    out.reverse()
    return out


async def safe_reply(event, text):
    return await core.safe_reply(event, text, REPLY_ATTEMPTS, recent_reply_ids)


async def edit_text(chat_id, msg_id, text, logger=None):
    return await core.edit_text(
        get_client(), chat_id, msg_id, text, REPLY_ATTEMPTS, logger
    )


def models_text(chat_id) -> str:
    current = model_for(chat_id)
    return core.models_text(current, MODELS)


async def refresh_models():
    global MODELS
    try:
        models = await asyncio.wait_for(ai.models.list(), timeout=REQUEST_TIMEOUT)
        data = getattr(models, "data", None) or ()
        ids = [getattr(m, "id", None) for m in data]
        ids = [i for i in ids if isinstance(i, str) and i]
        if ids:
            MODELS = ids
            logger.info("Загружено %d моделей из DanyAPI", len(ids))
    except (
        OpenAIError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        asyncio.TimeoutError,
    ) as exc:
        logger.warning("Не удалось загрузить модели из DanyAPI: %s", exc)


HELP_TEXT = (
    "Алиасы триггера / Trigger aliases: .db .ai\n"
    "Команды / Commands:\n"
    ".db <текст/text> - вопрос / question\n"
    ".db model <id> - сменить модель / set model\n"
    ".db models - список моделей / list models\n"
    ".db model - показать текущую модель / show current model\n"
    ".db clear - очистить контекст / clear context\n"
    ".db coder - кодер-режим живёт в боте / coder mode lives in the bot\n"
    ".db reasoning on/off - показ рассуждений / show reasoning\n"
    ".db tools on/off - показ вызовов инструментов / show tool calls\n"
    ".db prompt - системный промпт, только владелец / system prompt, owner only\n"
    ".db settings - текущие настройки, только владелец / settings, owner only\n"
    ".db help - эта справка / this help\n"
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
    triggered = bool(TRIGGER_RE.search(text))
    now = time.monotonic()

    try:
        command = handle_commands(text)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        command = None

    if command and command[0] in ("coder", "coder_status"):
        await safe_reply(event, "Кодер-режим работает только в боте: /coder on")
        return

    if command and command[0] == "prompt":
        if sender_id not in OWNER_IDS:
            await safe_reply(event, "Системный промпт доступен только владельцу.")
            return
        await safe_reply(event, system_prompt_report(chat_id, mode="userbot"))
        return

    if command and command[0] == "settings":
        if sender_id not in OWNER_IDS:
            await safe_reply(event, "Настройки доступны только владельцу.")
            return
        await safe_reply(event, settings_report(chat_id))
        return

    if command and command[0] in core.VISIBILITY_COMMANDS:
        if sender_id not in OWNER_IDS:
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
        if command[0] in core.OWNER_COMMANDS and sender_id not in OWNER_IDS:
            await safe_reply(event, "Команда доступна только владельцу.")
            return
        resp = core.handle_command_state(
            command,
            chat_id,
            is_private,
            chat_history,
            model_overrides,
            DM_HISTORY_LIMIT,
            GROUP_HISTORY_LIMIT,
            DANYAPI_MODEL,
            MODELS,
            HELP_TEXT,
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
        lambda t, tr: TRIGGER_RE.sub("", t, count=1).strip() if tr else t.strip(),
        strip_role_tag,
        lambda: get_sender_label(event),
        DM_HISTORY_LIMIT,
        GROUP_HISTORY_LIMIT,
        HISTORY_SAVER.mark_dirty,
    )

    if not triggered:
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
    prompt = core.compose_prompt(prompt, replied_text, MAX_REQUEST_LEN)

    if not prompt:
        return

    if len(prompt) > MAX_REQUEST_LEN:
        prompt = prompt[:MAX_REQUEST_LEN]

    model = model_for(chat_id)

    def system_fn(_cid):
        return system_for(_cid, mode="userbot")

    limit = DM_HISTORY_LIMIT if is_private else GROUP_HISTORY_LIMIT
    if not is_private:
        label = await get_sender_label(event)
        user_content = f"{label}: {prompt}" if label else prompt
        await core.append_group_history(
            chat_id, user_content, chat_history, GROUP_HISTORY_LIMIT, ctx_lock
        )
        HISTORY_SAVER.mark_dirty()
    live = await fetch_live_messages(chat_id, LIVE_HISTORY_LIMIT)
    if not live:
        live = [{"role": "user", "content": prompt}]
    messages = [{"role": "system", "content": system_fn(chat_id)}, *live]

    if is_self:
        prefix = f"{text}\n\n{model}:\n\n"
        self_edit_id = msg_id
    else:
        prefix = f"{model}:\n\n"
        self_edit_id = None

    delivery: dict[str, bool] = {}
    tool_menu = TOOLS if sender_id in OWNER_IDS else tools_module.PUBLIC_TOOLS
    owner = sender_id in OWNER_IDS
    progress: dict[str, str] = {"reason": ""}
    task_token: list[Any] = [None]

    def on_progress(rounds, calls_made, reason=""):
        if reason:
            progress["reason"] = reason
        TASKS.progress(chat_id, rounds=rounds, tools=calls_made, token=task_token[0])

    async def generate():
        record = TASKS.begin(chat_id, prompt, model=model, owner=owner)
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
                    render_response, reasoning_hidden, tools_hidden, chat_id
                ),
                edit_fn=edit_text,
                reply_fn=safe_reply,
                action=cast(Any, get_client().action(chat_id, "typing")),
                stream_fn=stream_with_tools,
                tool_client=None,
                tools=tool_menu,
                edit_interval=EDIT_INTERVAL,
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
                chat_history.setdefault(chat_id, deque(maxlen=limit)).append(
                    {"role": "assistant", "content": answer}
                )
            HISTORY_SAVER.mark_dirty()
        return answer

    task = SESSIONS.start(
        chat_id, generate(), logger=logger, scope="owner" if owner else "other"
    )
    try:
        await task
    except asyncio.CancelledError:
        if core.is_own_cancellation():
            raise
        logger.info("Запрос в чате %s прерван, жду новый", chat_id)
    except HANDLER_ERRORS:
        logger.exception("Ошибка генерации ответа")
        if not delivery.get("delivered"):
            await safe_reply(event, core.ERROR_NOTICE)


async def disconnect_quietly(timeout=10):
    SESSIONS.cancel_all(reason="остановка", logger=logger)
    await SESSIONS.drain(timeout)
    with contextlib.suppress(Exception):
        await HISTORY_SAVER.flush()
    if client is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(cast(Any, client.disconnect()), timeout=timeout)


async def close_ai():
    with contextlib.suppress(Exception):
        await cast(Any, ai.close())


async def _proxy_candidates():
    try:
        return await proxies.get_proxy_candidates(limit=40)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        logger.warning("Не удалось получить прокси: %r", exc)
        return []


CONNECT_TIMEOUT = 25


async def _try_connect(cli, proxy):
    if proxy:
        cli.set_proxy(proxy)
    start_coro = cast(Any, cli.start())
    await asyncio.wait_for(start_coro, timeout=CONNECT_TIMEOUT)
    return await cli.get_me()


async def start_userbot():
    global client
    if not API_ID or not API_HASH:
        logger.error("ENABLE_USERBOT=1, но API_ID/API_HASH не заданы в .env")
        return
    candidates = await _proxy_candidates()
    if not candidates:
        logger.warning("Нет прокси, пробую напрямую")
        candidates = [None]

    for dc in core.dc_candidates():
        client = None
        cli = get_client(dc["dc"] or None)
        cli.add_event_handler(handler, events.NewMessage(incoming=None))
        for idx, proxy in enumerate(candidates):
            if proxy:
                logger.info(
                    "ДЦ %s, прокси %d/%d: %s",
                    dc["address"],
                    idx + 1,
                    len(candidates),
                    proxy,
                )
            try:
                me = await _try_connect(cli, proxy)
                logger.info(
                    "Бот запущен как %s (@%s) через ДЦ %s",
                    getattr(me, "first_name", "?"),
                    getattr(me, "username", "?"),
                    dc["address"],
                )
                await cast(Any, cli.run_until_disconnected())
                return
            except AuthKeyError as exc:
                logger.error("Сессия невалидна, подключение прервано: %s", exc)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(cast(Any, cli.disconnect()), timeout=10)
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
                    "Не удалось подключиться через %s (%s): %s",
                    proxy,
                    dc["address"],
                    type(exc).__name__,
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(cast(Any, cli.disconnect()), timeout=10)
                if proxy:
                    proxies.mark_bad_proxy(proxies.telethon_to_item(proxy))
        logger.warning("ДЦ %s недоступен, пробую следующий", dc["address"])
        client = None

    logger.error("Не удалось подключиться ни через один ДЦ и прокси")
