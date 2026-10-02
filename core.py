import asyncio
import contextlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from dotenv import load_dotenv
from telethon.errors import FloodWaitError, RPCError

load_dotenv()

TRIGGER_ALIASES = (
    ".db",
    ".ai",
)

AUTO_ON_WORDS = ("on", "1", "true", "yes")
AUTO_OFF_WORDS = ("off", "0", "false", "no")


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
    return raw.lower() not in AUTO_OFF_WORDS


def _bool_command(name, arg):
    low = arg.strip().lower()
    if low in AUTO_ON_WORDS:
        return (name, True)
    if low in AUTO_OFF_WORDS:
        return (name, False)
    return (f"{name}_status", None)


VISIBILITY_COMMANDS = ("reasoning", "reasoning_status", "tools", "tools_status")

OWNER_COMMANDS = ("clear", "model", "models", "task")

VISIBILITY_LABELS = {
    "reasoning": "Рассуждения / Reasoning",
    "tools": "Инструменты / Tools",
}


def apply_visibility_command(command, chat_id, reasoning_hidden, tools_hidden):
    base = command[0].split("_")[0]
    target = reasoning_hidden if base == "reasoning" else tools_hidden
    label = VISIBILITY_LABELS[base]
    if command[0].endswith("_status"):
        state = "скрыты" if chat_id in target else "показаны"
        return (f"{label}: {state}", False)
    if command[1]:
        target.discard(chat_id)
        state = "показаны"
    else:
        target.add(chat_id)
        state = "скрыты"
    return (f"{label}: {state}", True)


def make_render(render_fn, reasoning_hidden, tools_hidden, chat_id):
    show_reasoning = chat_id not in reasoning_hidden
    show_tools = chat_id not in tools_hidden

    def render(prefix, reasoning_parts, tool_parts, answer):
        return render_fn(
            prefix, reasoning_parts, tool_parts, answer, show_reasoning, show_tools
        )

    return render


def _alias_pattern(aliases):
    return r"(?:" + "|".join(re.escape(a) for a in aliases) + r")"


def _build_trigger_re():
    return re.compile(
        r"(?<![a-zа-я0-9])" + _alias_pattern(TRIGGER_ALIASES) + r"(?![a-zа-я0-9])",
        re.IGNORECASE,
    )


TRIGGER_RE = _build_trigger_re()

DC_MAIN = "149.154.167.220"
DC_MAIN_ENV = "DC_MAIN"
DC_FALLBACK = DC_MAIN
DC_FALLBACK_ENV = "DC_FALLBACK"
DC_DISABLED_ENV = "DC_DISABLED"
DC_ORDER_ENV = "DC_ORDER"

DC_ADDRESSES = {
    1: "149.154.175.53",
    2: DC_MAIN,
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}

DC_ORDER = (2, 1, 3, 4, 5)

DC_PORT = 443

API_HOST = "api.telegram.org"
API_BASE_URL = f"https://{API_HOST}"

DRAIN_TIMEOUT = 5.0


def _dc_int_set(name: str) -> set[int]:
    raw = _env_str(name, "")
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        chunk = part.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            continue
        if value in DC_ADDRESSES:
            out.add(value)
    return out


def _dc_order_from_env() -> list[int]:
    parsed: list[int] = []
    for part in _env_str(DC_ORDER_ENV, "").replace(";", ",").split(","):
        chunk = part.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            continue
        if value in DC_ADDRESSES and value not in parsed:
            parsed.append(value)
    return parsed


def dc_order() -> list[int]:
    return _dc_order_from_env() or list(DC_ORDER)


def dc_main() -> str:
    return _env_str(DC_MAIN_ENV, DC_MAIN) or DC_MAIN


def dc_fallback() -> str:
    return _env_str(DC_FALLBACK_ENV, DC_MAIN) or DC_MAIN


def dc_address(dc=None) -> str:
    if dc is None:
        return ""
    if isinstance(dc, bool):
        return ""
    try:
        value = int(dc)
    except (TypeError, ValueError):
        return ""
    if isinstance(dc, float) and value != dc:
        return ""
    if isinstance(dc, str) and str(value) != dc.strip():
        return ""
    return DC_ADDRESSES.get(value, "")


def dc_api_url(dc=None) -> str | None:
    if not dc_address(dc):
        return None
    return API_BASE_URL


def dc_api_pin(dc=None) -> str:
    if dc is None:
        return dc_fallback()
    return dc_address(dc)


def dc_candidates(extra=()) -> list[dict]:
    disabled = _dc_int_set(DC_DISABLED_ENV)
    order = dc_order()
    for item in extra:
        value = int(item) if str(item).strip().isdigit() else -1
        if value in DC_ADDRESSES and value not in order:
            order.append(value)
    out = [
        {"dc": value, "address": DC_ADDRESSES[value]}
        for value in order
        if value not in disabled
    ]
    seen = {item["address"] for item in out}
    for address in (dc_main(), dc_fallback()):
        if address and address not in seen:
            out.append({"dc": 0, "address": address})
            seen.add(address)
    preferred: list[str] = []
    for address in (dc_main(), dc_fallback()):
        if address and address not in preferred:
            preferred.append(address)
    out.sort(key=lambda item: item["address"] not in preferred)
    return out


SUB_ALIASES = {
    "clear": ("clear",),
    "model": ("model",),
    "models": ("models",),
    "help": ("help", "?"),
    "coder": ("coder",),
    "reasoning": ("reasoning",),
    "tools": ("tools",),
    "prompt": ("prompt",),
    "settings": ("settings",),
    "task": ("task",),
}

_SUB_LOOKUP = {
    alias: name for name, aliases in SUB_ALIASES.items() for alias in aliases
}

_ALL_ALIASES = tuple(sorted(TRIGGER_ALIASES, key=len, reverse=True))


def _strip_alias_prefix(low):
    for a in _ALL_ALIASES:
        if low.startswith(a):
            tail = low[len(a) :]
            if tail and not tail[0].isspace():
                continue
            return tail.strip(), a
    return None, None


def handle_commands(text) -> tuple[str, Any] | None:
    low = text.strip().lower()
    if not low.startswith("."):
        return None

    rest, alias = _strip_alias_prefix(low)
    if alias is None or not rest:
        return None

    parts = rest.split(maxsplit=1)
    sub = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    cmd = _SUB_LOOKUP.get(sub)
    if cmd is None:
        return None
    if cmd in ("coder", "reasoning", "tools"):
        return _bool_command(cmd, arg)
    if cmd == "model":
        return ("model", arg or None)
    return (cmd, None)


BOT_COMMANDS = {
    "start": ("start", "help", "?"),
    "clear": ("clear",),
    "model": ("model",),
    "models": ("models",),
    "settings": ("settings",),
    "coder": ("coder",),
    "reasoning": ("reasoning",),
    "tools": ("tools",),
    "prompt": ("prompt",),
    "task": ("task",),
}

_BOT_CMD_LOOKUP = {
    alias: name for name, aliases in BOT_COMMANDS.items() for alias in aliases
}


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
    if cmd in ("coder", "reasoning", "tools"):
        return _bool_command(cmd, arg)
    if cmd == "model":
        return ("model", arg or None)
    if cmd == "models":
        return ("models", None)
    if cmd == "start":
        return ("help", None)
    return (cmd, None)


BOT_COMMAND_TITLES = {
    "help": "Справка / Help",
    "clear": "Очистить контекст / Clear",
    "model": "Текущая модель / Current model",
    "models": "Список моделей / Models",
    "settings": "Настройки / Settings",
    "coder": "Кодер-режим / Coder mode",
    "reasoning": "Рассуждения / Reasoning",
    "tools": "Инструменты / Tools",
    "prompt": "Системный промпт / System prompt",
    "task": "Журнал задач / Task log",
}

INLINE_OWNER_COMMANDS = (
    "clear",
    "model",
    "models",
    "settings",
    "coder",
    "reasoning",
    "tools",
    "prompt",
    "task",
)


def inline_command_matches(query, is_owner) -> list[tuple[str, str]]:
    stripped = (query or "").strip()
    if not stripped.startswith("/"):
        return []
    parts = stripped[1:].split()
    if not parts:
        return []
    head = parts[0].split("@", 1)[0].strip().lower()
    if not head:
        return []
    parsed = handle_bot_commands(stripped)
    if parsed is None:
        names = [name for name in BOT_COMMAND_TITLES if name.startswith(head)]
    else:
        name = parsed[0].removesuffix("_status")
        names = [name] if name in BOT_COMMAND_TITLES else []
    return [
        (f"/{name}", BOT_COMMAND_TITLES[name])
        for name in names
        if is_owner or name not in INLINE_OWNER_COMMANDS
    ]


INLINE_MARK = "\u2063"
INLINE_TOKEN_LEN = 12
INLINE_MARK_RE = re.compile(
    f"{INLINE_MARK}([0-9a-f]{{{INLINE_TOKEN_LEN}}}){INLINE_MARK}"
)
INLINE_MARK_WIDTH = 2 * len(INLINE_MARK) + INLINE_TOKEN_LEN + 1

INLINE_TOKEN_TTL = 300.0


def new_inline_token() -> str:
    return secrets.token_hex(INLINE_TOKEN_LEN // 2)


def inline_marked_text(token: str, text: str) -> str:
    mark = f"{INLINE_MARK}{token}{INLINE_MARK}"
    body = (text or "").strip()
    return f"{mark} {body}" if body else mark


def inline_token_of(text) -> str | None:
    match = INLINE_MARK_RE.search(text or "")
    return match.group(1) if match else None


def strip_inline_mark(text) -> str:
    return INLINE_MARK_RE.sub("", text or "", count=1).strip()


def strip_role_tag(text: str, bot_name: str = "DanyBOT") -> str:
    low = text.strip().lower()
    if low.startswith(f"{bot_name.lower()}:"):
        return text.split(":", 1)[1].strip()
    m = re.match(r"^\[(user|assistant|system)\]\s*", text, re.IGNORECASE)
    if m:
        return text[m.end() :]
    return text


def models_text(current: str, models) -> str:
    parts = ["Доступные модели / Available models:"]
    for m in models:
        marker = " (текущая) / current" if m == current else ""
        parts.append(f"• {m}{marker}")
    if current not in models:
        parts.append(f"• {current} (текущая) / current")
    return "\n".join(parts)


def _int_map(raw) -> dict[int, str]:
    out: dict[int, str] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        try:
            out[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return out


def _int_set(raw) -> set[int]:
    out: set[int] = set()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return out
    for item in raw:
        try:
            out.add(int(item))
        except (TypeError, ValueError):
            continue
    return out


def parse_state_data(data):
    if not isinstance(data, dict):
        raise TypeError("state must be an object")
    state = {
        "model_overrides": _int_map(data.get("model_overrides", {})),
        "coder_chats": _int_set(data.get("coder_chats", [])),
        "reasoning_hidden": _int_set(data.get("reasoning_hidden", [])),
        "tools_hidden": _int_set(data.get("tools_hidden", [])),
        "tasks": data.get("tasks", {}),
    }
    if isinstance(data.get("inline_mode"), bool):
        state["inline_mode"] = data["inline_mode"]
    return state


def load_state_file(path):
    if not path.exists():
        return None
    try:
        return parse_state_data(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError, KeyError, TypeError, AttributeError):
        return None


_WRITE_LOCK = threading.Lock()

_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY = 0.05


def _replace_with_retry(tmp_name, path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_name, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY)


def _build_payload(builder) -> str:
    attempt = 0
    while True:
        try:
            return builder()
        except RuntimeError:
            attempt += 1
            if attempt >= _REPLACE_ATTEMPTS:
                raise
            time.sleep(_REPLACE_DELAY)


def _fsync_dir(directory) -> None:
    if os.name == "nt":
        return
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_text_atomic(path, text) -> bool:
    tmp_name = None
    try:
        with _WRITE_LOCK:
            payload = _build_payload(text) if callable(text) else text
            handle, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
            )
            os.close(handle)
            with open(tmp_name, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if path.is_file():
                os.chmod(tmp_name, stat.S_IMODE(path.stat().st_mode))
            _replace_with_retry(tmp_name, path)
            tmp_name = None
            _fsync_dir(path.parent)
        return True
    except (OSError, TypeError, ValueError, RuntimeError, KeyError, AttributeError):
        return False
    finally:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def save_state_file(path, state):
    def snapshot():
        data = {
            "model_overrides": {str(k): v for k, v in state["model_overrides"].items()},
            "coder_chats": sorted(state.get("coder_chats", [])),
            "reasoning_hidden": sorted(state.get("reasoning_hidden", [])),
            "tools_hidden": sorted(state.get("tools_hidden", [])),
        }
        if state.get("tasks"):
            data["tasks"] = state["tasks"]
        if isinstance(state.get("inline_mode"), bool):
            data["inline_mode"] = state["inline_mode"]
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    return _write_text_atomic(path, snapshot)


def load_history_file(path, dm_limit, group_limit):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        return None
    if not isinstance(data, dict):
        return None
    history = {}
    for key, value in data.items():
        try:
            chat_id = int(key)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, (list, tuple)):
            continue
        limit = dm_limit if chat_id > 0 else group_limit
        history[chat_id] = deque(value, maxlen=limit)
    return history


def save_history_file(path, history):
    def snapshot():
        data = {str(k): list(v) for k, v in history.items()}
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    return _write_text_atomic(path, snapshot)


_MODE_KEYS = (
    "model_overrides",
    "coder_chats",
    "reasoning_hidden",
    "tools_hidden",
    "chat_history",
    "ctx_lock",
    "recent_reply_ids",
    "last_chat_activity",
    "seen_msg_keys",
    "inline_mode",
)

SAVER_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    TypeError,
    RuntimeError,
    KeyError,
)

STREAM_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    AttributeError,
    RuntimeError,
    RecursionError,
    MemoryError,
)


class AsyncSaver:
    _task: Any | None
    _dirty: bool

    def __init__(self, writer: Callable[[], Any], delay=0.5, logger=None):
        self._writer = writer
        self._delay = delay
        self._logger = logger
        self._task = None
        self._dirty = False

    def mark_dirty(self):
        self._dirty = True
        if self._task is not None and not self._task.done():
            return
        coro = self._run()
        try:
            self._task = asyncio.create_task(coro)
        except RuntimeError:
            coro.close()

    async def _write(self) -> bool:
        try:
            result = await asyncio.to_thread(self._writer)
        except SAVER_ERRORS as exc:
            if self._logger:
                self._logger.warning("AsyncSaver write failed: %r", exc)
            return False
        return result is not False

    async def _run(self):
        while True:
            try:
                await asyncio.sleep(self._delay)
            except asyncio.CancelledError:
                return
            self._dirty = False
            try:
                written = await self._write()
            except asyncio.CancelledError:
                self._dirty = True
                raise
            if not written:
                self._dirty = True
                return
            if not self._dirty:
                return

    async def _write_dirty(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if not await self._write():
            self._dirty = True

    async def flush(self):
        task = self._task
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=self._delay + 10)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._write_dirty()


class ModeStore:
    _ns: dict[str, Any]

    def __init__(self, ns):
        self._ns = ns

    def __getattr__(self, name):
        if name in _MODE_KEYS:
            try:
                return self._ns[name]
            except KeyError as exc:
                raise AttributeError(name) from exc
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in _MODE_KEYS:
            self._ns[name] = value
        else:
            object.__setattr__(self, name, value)


def load_state_into(store, state_file, logger=None, journal=None):
    state = load_state_file(state_file)
    if state is None:
        if logger is not None and state_file.exists():
            logger.warning(
                "Не удалось прочитать %s, состояние сброшено", state_file.name
            )
        return
    store.model_overrides = state["model_overrides"]
    store.coder_chats = state["coder_chats"]
    store.reasoning_hidden = state["reasoning_hidden"]
    store.tools_hidden = state["tools_hidden"]
    if "inline_mode" in state:
        store.inline_mode = state["inline_mode"]
    if journal is not None:
        journal.restore(state.get("tasks"))


def save_state_from(store, state_file, logger, journal=None) -> bool:
    payload = {
        "model_overrides": store.model_overrides,
        "coder_chats": store.coder_chats,
        "reasoning_hidden": store.reasoning_hidden,
        "tools_hidden": store.tools_hidden,
    }
    inline = getattr(store, "inline_mode", None)
    if isinstance(inline, bool):
        payload["inline_mode"] = inline
    if journal is not None:
        payload["tasks"] = journal.snapshot()
    if save_state_file(state_file, payload):
        return True
    logger.warning("Не удалось сохранить %s", state_file.name)
    return False


def load_history_into(store, history_file, dm_limit, group_limit, logger=None):
    history = load_history_file(history_file, dm_limit, group_limit)
    if history is None:
        if logger is not None and history_file.exists():
            logger.warning(
                "Не удалось прочитать %s, история сброшена", history_file.name
            )
        return
    store.chat_history = history


def save_history_from(store, history_file, logger) -> bool:
    if save_history_file(history_file, store.chat_history):
        return True
    logger.warning("Не удалось сохранить %s", history_file.name)
    return False


MAX_RECENT_IDS = 5000
MAX_SEEN_MSG_KEYS = 20000
_EVICT_BATCH = 1000


def _evict_oldest(target, limit: int) -> None:
    overflow = len(target) - limit
    if overflow <= 0:
        return
    mapping = isinstance(target, dict)
    for key in list(target)[: min(overflow, _EVICT_BATCH)]:
        if mapping:
            target.pop(key, None)
        else:
            target.discard(key)


def _remember_key(target, key, limit: int) -> None:
    target.add(key)
    _evict_oldest(target, limit)


async def safe_reply(event, text, attempts, recent_ids):
    if not text or not text.strip():
        text = "…"
    for _attempt in range(attempts):
        try:
            sent = await event.reply(text)
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 30))
            continue
        except (RPCError, OSError, ValueError, TypeError):
            return None
        if sent:
            _remember_key(
                recent_ids, (getattr(event, "chat_id", None), sent.id), MAX_RECENT_IDS
            )
        return sent
    return None


async def edit_text(client, chat_id, msg_id, text, attempts, logger=None):
    if not text or not text.strip():
        if logger is not None:
            logger.warning("edit_message пропущен: пустой текст")
        return False
    last_error = ""
    for attempt in range(attempts):
        try:
            await client.edit_message(chat_id, msg_id, text)
            return True
        except FloodWaitError as e:
            last_error = f"FloodWait {e.seconds}s"
            if logger is not None:
                logger.warning(
                    "edit_message флуд-лимит: %ss, попытка %d/%d",
                    e.seconds,
                    attempt + 1,
                    attempts,
                )
            await asyncio.sleep(min(e.seconds, 30))
            continue
        except (RPCError, OSError, ValueError, TypeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if logger is not None:
                logger.warning("edit_message ошибка: %s", last_error)
            return False
    if logger is not None:
        logger.warning(
            "edit_message не удался после %d попыток: %s", attempts, last_error
        )
    return False


async def fetch_replied_message(message):
    try:
        if not message.is_reply:
            return None
        return await message.get_reply_message()
    except (RPCError, OSError, ValueError, TypeError, AttributeError):
        return None


async def fetch_replied_text(message):
    reply_msg = await fetch_replied_message(message)
    if reply_msg and reply_msg.message:
        return reply_msg.message.strip()
    return None


QUOTE_HEADER = "Сообщение, на которое ответили:\n{quoted}\n\nЗапрос: "

ERROR_NOTICE = "Ошибка при обращении к DanyAPI. / DanyAPI request error."

INTERRUPTED_NOTICE = "Запрос прерван новым сообщением. / Request interrupted."

EMPTY_ANSWER = "(пустой ответ / empty answer)"

TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_INTERRUPTED = "interrupted"
TASK_STOPPED = "stopped"
TASK_FAILED = "failed"

TASK_STATUSES = frozenset(
    {TASK_RUNNING, TASK_DONE, TASK_INTERRUPTED, TASK_STOPPED, TASK_FAILED}
)

TASK_LABELS = {
    TASK_RUNNING: "В работе",
    TASK_DONE: "Завершена",
    TASK_INTERRUPTED: "Прервана",
    TASK_STOPPED: "Остановлена по лимиту",
    TASK_FAILED: "Упала с ошибкой",
}

TASK_SHORT_LABELS = {
    TASK_RUNNING: "в работе",
    TASK_DONE: "завершена",
    TASK_INTERRUPTED: "прервана",
    TASK_STOPPED: "остановлена по лимиту",
    TASK_FAILED: "упала",
}


def task_state_label(task) -> str:
    if not task:
        return "нет"
    label = TASK_SHORT_LABELS.get(task["status"], str(task["status"])[:20])
    if task["rounds"] or task["tools"]:
        return f"{label} ({task['rounds']} раундов, {task['tools']} вызовов)"
    return label


TASK_PROMPT_CHARS = 800
TASK_REASON_CHARS = 200


def compose_prompt(prompt, replied_text, limit):
    if not replied_text:
        return prompt
    quoted = replied_text[: max(1, limit // 2)]
    if not prompt:
        return quoted
    header = QUOTE_HEADER.format(quoted=quoted)
    if len(header) >= limit:
        room = max(0, limit - len(QUOTE_HEADER.format(quoted="")))
        header = QUOTE_HEADER.format(quoted=replied_text[:room])
    return (header + prompt)[:limit]


def check_cooldown(
    chat_id,
    now,
    cooldown,
    last_chat_activity,
    cleanup_threshold=10000,
    cleanup_age=3600,
):
    if len(last_chat_activity) > cleanup_threshold:
        cutoff = now - cleanup_age
        for k in list(last_chat_activity):
            if k == chat_id:
                continue
            if last_chat_activity[k] < cutoff:
                del last_chat_activity[k]
    if cooldown > 0:
        last = last_chat_activity.get(chat_id)
        if last is not None and (now - last) < cooldown:
            return True
    last_chat_activity[chat_id] = now
    return False


def history_limit(chat_id, is_private, dm_limit, group_limit) -> int:
    private = chat_id > 0 if is_private is None else bool(is_private)
    return dm_limit if private else group_limit


def history_for(chat_history, chat_id, limit) -> deque:
    hist = chat_history.get(chat_id)
    if hist is not None and hist.maxlen == limit:
        return hist
    fresh = deque(hist if hist is not None else (), maxlen=limit)
    chat_history[chat_id] = fresh
    return fresh


def handle_command_state(
    command,
    chat_id,
    is_private,
    chat_history,
    model_overrides,
    dm_limit,
    group_limit,
    default_model,
    models_list,
    help_text,
    task_text="",
) -> tuple[str, bool, bool] | None:
    cmd = command[0]
    if cmd == "clear":
        limit = history_limit(chat_id, is_private, dm_limit, group_limit)
        hist = history_for(chat_history, chat_id, limit)
        hist.clear()
        return ("Контекст очищен. / Context cleared.", False, True)
    if cmd == "model":
        val = command[1]
        if val:
            if models_list and val not in models_list:
                return (models_text(val, models_list), False, False)
            model_overrides[chat_id] = val
            return (f"Модель установлена / Model set: {val}", True, False)
        current = model_overrides.get(chat_id, default_model)
        return (f"Текущая модель / Current model: {current}", False, False)
    if cmd == "models":
        current = model_overrides.get(chat_id, default_model)
        return (models_text(current, models_list), False, False)
    if cmd == "help":
        return (help_text, False, False)
    if cmd == "task":
        return (task_text or "Журнала задач пока нет. / No tasks yet.", False, False)
    return None


async def append_group_history(chat_id, content, chat_history, group_limit, ctx_lock):
    async with ctx_lock:
        hist = history_for(chat_history, chat_id, group_limit)
        hist.append({"role": "user", "content": content})


def make_stream_callbacks(
    prefix, render_fn, edit_text_fn, chat_id, edit_interval, tool_edit_interval=5.0
):
    state = {
        "answer_parts": [],
        "last_edit": 0.0,
        "reasoning_parts": [],
        "tool_parts": [],
        "edit_id": None,
    }

    def render():
        return render_fn(
            prefix,
            state["reasoning_parts"],
            state["tool_parts"],
            "".join(state["answer_parts"]),
        )

    async def _maybe_edit(min_interval=None):
        now = time.monotonic()
        wait = edit_interval
        if min_interval is not None:
            wait = max(edit_interval, min_interval)
        if state["edit_id"] is not None and (now - state["last_edit"]) >= wait:
            state["last_edit"] = now
            await edit_text_fn(chat_id, state["edit_id"], render())

    async def on_delta(part):
        state["answer_parts"].append(part)
        await _maybe_edit()

    async def on_reasoning(part):
        state["reasoning_parts"].append(part)
        await _maybe_edit()

    async def on_tool(name):
        state["tool_parts"].append(name)
        await _maybe_edit(tool_edit_interval)

    return state, render, on_delta, on_reasoning, on_tool


async def append_message_context(
    store,
    chat_id,
    msg_id,
    is_private,
    text,
    is_self,
    triggered,
    strip_trigger_fn,
    strip_role_fn,
    sender_label_fn,
    dm_limit,
    group_limit,
    save_history_fn,
):
    if not is_private or text.strip() == "…":
        return
    key = (chat_id, msg_id)
    if key in store.seen_msg_keys:
        return
    _remember_key(store.seen_msg_keys, key, MAX_SEEN_MSG_KEYS)
    try:
        stripped = strip_trigger_fn(text, triggered) or text.strip()
        if is_self:
            role = "user" if triggered else "assistant"
            content = strip_role_fn(stripped)
        else:
            role = "user"
            label = await sender_label_fn()
            content = f"{label}: {stripped}" if label else stripped
        limit = history_limit(chat_id, is_private, dm_limit, group_limit)
        async with store.ctx_lock:
            hist = history_for(store.chat_history, chat_id, limit)
            hist.append({"role": role, "content": content})
    except BaseException:
        store.seen_msg_keys.discard(key)
        raise
    save_history_fn()


async def prepare_messages(
    store, chat_id, dm_limit, group_limit, system_fn, is_private=None
):
    limit = history_limit(chat_id, is_private, dm_limit, group_limit)
    async with store.ctx_lock:
        hist = history_for(store.chat_history, chat_id, limit)
        return [{"role": "system", "content": system_fn(chat_id)}, *list(hist)]


def trim_tool_history(messages, max_messages, head_size=1):
    if max_messages < 2 or len(messages) <= max_messages:
        return messages
    head = messages[:head_size]
    start = max(head_size, len(messages) - (max_messages - head_size))
    while start < len(messages) and messages[start].get("role") == "tool":
        start += 1
    if start >= len(messages):
        return messages
    return [*head, *messages[start:]]


def is_own_cancellation() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _run_after(coro, previous):
    pending = {task for task in previous if not task.done()}
    while pending:
        _done, pending = await asyncio.wait(pending)
    return await coro


class SessionRegistry:
    def __init__(self):
        self._slots: dict[Any, dict[str, Any]] = {}
        self._draining: set[Any] = set()

    def _live(self, chat_id) -> list[Any]:
        slot = self._slots.get(chat_id)
        if slot is None:
            return []
        return [task for task in slot["tasks"] if not task.done()]

    def _owner_live(self, slot, live) -> bool:
        scopes = slot["scopes"]
        return any(scopes.get(id(task)) == "owner" for task in live)

    def _drop(self, chat_id, task, coro) -> None:
        self._draining.discard(task)
        slot = self._slots.get(chat_id)
        if slot is not None:
            slot["scopes"].pop(id(task), None)
            if task in slot["tasks"]:
                slot["tasks"].remove(task)
            if not slot["tasks"] and self._slots.get(chat_id) is slot:
                del self._slots[chat_id]
        with contextlib.suppress(Exception):
            coro.close()

    def _watcher(self, chat_id, coro):
        def _on_done(task) -> None:
            self._drop(chat_id, task, coro)

        return _on_done

    def start(self, chat_id, coro, logger=None, scope="other"):
        live = self._live(chat_id)
        slot = self._slots.get(chat_id)
        if not live:
            task = asyncio.ensure_future(coro)
        elif scope != "owner" and slot is not None and self._owner_live(slot, live):
            if logger is not None:
                logger.info(
                    "Запрос в чате %s ждёт завершения работы владельца", chat_id
                )
            task = asyncio.ensure_future(_run_after(coro, live))
        else:
            self.cancel(chat_id, reason="новый запрос", logger=logger)
            slot = None
            task = asyncio.ensure_future(coro)
        if slot is None:
            slot = {"tasks": [], "scopes": {}}
            self._slots[chat_id] = slot
        slot["tasks"].append(task)
        slot["scopes"][id(task)] = scope
        task.add_done_callback(self._watcher(chat_id, coro))
        return task

    def cancel(self, chat_id, reason="", logger=None) -> bool:
        slot = self._slots.pop(chat_id, None)
        if slot is None:
            return False
        stopped = False
        for task in list(slot["tasks"]):
            if task.done():
                continue
            self._draining.add(task)
            task.cancel()
            stopped = True
        if stopped and logger is not None:
            logger.info("Прерван запрос в чате %s: %s", chat_id, reason or "причина")
        return stopped

    def cancel_all(self, reason="остановка", logger=None) -> int:
        stopped = 0
        for chat_id in list(self._slots):
            if self.cancel(chat_id, reason=reason, logger=logger):
                stopped += 1
        return stopped

    async def drain(self, timeout=DRAIN_TIMEOUT) -> None:
        tasks = [task for task in self._draining if not task.done()]
        if not tasks:
            return
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout
            )

    def is_running(self, chat_id) -> bool:
        return bool(self._live(chat_id))

    def running_chats(self) -> int:
        return sum(
            1
            for slot in list(self._slots.values())
            if any(not task.done() for task in slot["tasks"])
        )


class TaskJournal:
    def __init__(self, save=None, limit: int = 40):
        self._save = save
        self._limit = max(1, limit)
        self._lock = threading.Lock()
        self._tasks: dict[int, dict[str, Any]] = {}
        self._seq = 0

    def _persist(self) -> None:
        if self._save is not None:
            self._save()

    def begin(self, chat_id, prompt, model="", coder=False, owner=False):
        with self._lock:
            self._seq += 1
            token = self._seq
        record = {
            "token": token,
            "prompt": str(prompt or "")[:TASK_PROMPT_CHARS],
            "model": str(model or "")[:80],
            "coder": bool(coder),
            "owner": bool(owner),
            "status": TASK_RUNNING,
            "reason": "",
            "rounds": 0,
            "tools": 0,
            "started": time.time(),
            "updated": time.time(),
        }
        with self._lock:
            self._tasks[chat_id] = record
            self._trim()
        self._persist()
        return record

    def progress(self, chat_id, persist: bool = False, token=None, **fields) -> None:
        with self._lock:
            record = self._tasks.get(chat_id)
            if record is None:
                return
            if token is not None and record.get("token") != token:
                return
            for key, value in fields.items():
                if key in record:
                    record[key] = value
            record["updated"] = time.time()
        if persist:
            self._persist()

    def finish(self, chat_id, status, reason="", token=None, **fields):
        with self._lock:
            record = self._tasks.get(chat_id)
            if record is None:
                return None
            if token is not None and record.get("token") != token:
                return None
            record["status"] = status
            record["reason"] = str(reason or "")[:TASK_REASON_CHARS]
            for key, value in fields.items():
                if key in record:
                    record[key] = value
            record["updated"] = time.time()
            snapshot = dict(record)
        self._persist()
        return snapshot

    def get(self, chat_id):
        with self._lock:
            record = self._tasks.get(chat_id)
            return dict(record) if record else None

    def recent(self, limit: int = 5):
        with self._lock:
            items = sorted(
                self._tasks.values(), key=lambda r: r["updated"], reverse=True
            )
        return [dict(item) for item in items[: max(1, limit)]]

    def _trim(self) -> None:
        overflow = len(self._tasks) - self._limit
        if overflow <= 0:
            return
        ordered = sorted(self._tasks.items(), key=lambda kv: kv[1]["updated"])
        for chat_id, record in ordered:
            if len(self._tasks) <= self._limit:
                return
            if record["status"] == TASK_RUNNING:
                continue
            self._tasks.pop(chat_id, None)
        ordered = sorted(self._tasks.items(), key=lambda kv: kv[1]["updated"])
        for chat_id, _record in ordered[: len(self._tasks) - self._limit]:
            self._tasks.pop(chat_id, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                str(chat_id): dict(record) for chat_id, record in self._tasks.items()
            }

    def restore(self, data) -> int:
        restored = 0
        now = time.time()
        rows: list[tuple[Any, Any]] = []
        if isinstance(data, dict):
            rows = list(data.items())
        elif isinstance(data, list):
            rows = [(row.get("chat_id"), row) for row in data if isinstance(row, dict)]
        with self._lock:
            self._tasks.clear()
            for chat_id, record in rows:
                clean = _clean_task_record(record)
                if clean is None:
                    continue
                if clean["status"] == TASK_RUNNING:
                    clean["status"] = TASK_INTERRUPTED
                    clean["reason"] = "процесс перезапущен"
                    clean["updated"] = now
                try:
                    key = int(chat_id)
                except (TypeError, ValueError):
                    continue
                self._tasks[key] = clean
                restored += 1
            self._seq = max(
                (int(item.get("token") or 0) for item in self._tasks.values()),
                default=0,
            )
            self._trim()
        return restored

    def report(self, chat_id) -> str:
        record = self.get(chat_id)
        if record is None:
            return "Задач в этом чате не было."
        return _task_report(record)


def _safe_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _clean_task_record(record) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    status = str(record.get("status", ""))[:20]
    if status not in TASK_STATUSES:
        return None
    clean = {
        "token": _safe_int(record.get("token")),
        "prompt": str(record.get("prompt", ""))[:TASK_PROMPT_CHARS],
        "model": str(record.get("model", ""))[:80],
        "coder": bool(record.get("coder", False)),
        "owner": bool(record.get("owner", False)),
        "status": status,
        "reason": str(record.get("reason", ""))[:TASK_REASON_CHARS],
        "rounds": _safe_int(record.get("rounds")),
        "tools": _safe_int(record.get("tools")),
        "started": _safe_float(record.get("started")),
        "updated": _safe_float(record.get("updated")),
    }
    return clean


def _task_report(record) -> str:
    started = _fmt_time(record["started"])
    updated = _fmt_time(record["updated"])
    flags = "".join(
        (
            " · кодер-режим" if record.get("coder") else "",
            " · владелец" if record.get("owner") else "",
        )
    )
    lines = [
        f"Задача: {TASK_LABELS.get(record['status'], record['status'])}",
        f"Модель: {record['model'] or '?'}{flags}",
        f"Начата: {started} · обновлена: {updated}",
        f"Раундов: {record['rounds']} · вызовов инструментов: {record['tools']}",
    ]
    if record["reason"]:
        lines.append(f"Причина: {record['reason']}")
    if record["prompt"]:
        lines.append(f"Текст: {record['prompt']}")
    return "\n".join(lines)


def _fmt_time(value) -> str:
    if not value:
        return "?"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    except (OSError, OverflowError, ValueError):
        return "?"


async def stream_answer(
    store,
    *,
    event,
    chat_id,
    is_self,
    messages,
    model,
    prefix,
    self_edit_id,
    render_fn,
    edit_fn,
    reply_fn,
    action,
    stream_fn,
    tool_client=None,
    tools=None,
    edit_interval=1.0,
    owner_id=None,
    logger=None,
    stats=None,
    delivery=None,
    progress_fn=None,
):
    import userbot as userbot_module

    unrestricted = owner_id is not None and owner_id in userbot_module.OWNER_IDS
    state, render, on_delta, on_reasoning, on_tool = make_stream_callbacks(
        prefix, render_fn, edit_fn, chat_id, edit_interval
    )
    placeholder_id = None
    if self_edit_id is not None:
        state["edit_id"] = self_edit_id
    error = None
    result = None
    delivered = False
    caught = (*STREAM_ERRORS, userbot_module.OpenAIError, RPCError)

    async def deliver(text) -> bool:
        nonlocal delivered
        if state["edit_id"] is not None:
            edited = await edit_fn(chat_id, state["edit_id"], text, logger=logger)
            delivered = bool(edited)
            if not edited:
                if logger is not None:
                    logger.warning(
                        "финальный edit не удался, отправляю ответ новым сообщением"
                    )
                delivered = bool(await reply_fn(event, text))
        elif not is_self:
            delivered = bool(await reply_fn(event, text))
        return delivered

    try:
        try:
            async with action:
                if not is_self:
                    placeholder = await reply_fn(event, "…")
                    if placeholder:
                        placeholder_id = placeholder.id
                        state["edit_id"] = placeholder.id
                        store.recent_reply_ids.add((chat_id, placeholder.id))
                result = await stream_fn(
                    messages,
                    model,
                    chat_id,
                    on_delta,
                    on_reasoning,
                    on_tool,
                    client_override=tool_client,
                    tools=tools,
                    verify_tools=not unrestricted,
                    sanitize_tools=userbot_module.SANITIZE_ENABLED,
                    unrestricted=unrestricted,
                    stats=stats,
                    on_progress=progress_fn,
                )
        except caught as exc:
            error = exc
        except asyncio.CancelledError:
            partial = "".join(state["answer_parts"])
            text = _final_text(state, render, partial, None)
            if text.strip() in ("", "…", EMPTY_ANSWER):
                text = INTERRUPTED_NOTICE
            else:
                text = f"{text}\n\n{INTERRUPTED_NOTICE}"
            if logger is not None:
                logger.info("Ответ в чате %s прерван", chat_id)
            with contextlib.suppress(Exception):
                await deliver(text)
            raise
        full_answer = result or "".join(state["answer_parts"])
        await deliver(_final_text(state, render, full_answer, error))
    finally:
        if placeholder_id is not None:
            store.recent_reply_ids.discard((chat_id, placeholder_id))
    if error is not None:
        if delivery is not None:
            delivery["delivered"] = delivered
        raise error
    return full_answer


def _final_text(state, render, full_answer, error):
    final_text = render()
    stripped = final_text.strip()
    if stripped and stripped != "…":
        text = final_text
    elif full_answer.strip():
        text = full_answer
    elif state["reasoning_parts"] or state["tool_parts"]:
        text = final_text
    elif error is not None:
        text = ERROR_NOTICE
    else:
        text = EMPTY_ANSWER
    if error is not None and text != ERROR_NOTICE:
        text = f"{text}\n\n{ERROR_NOTICE}"
    return text
