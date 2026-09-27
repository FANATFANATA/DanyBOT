import asyncio
import contextlib
import json
import os
import re
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

OWNER_COMMANDS = ("clear", "model")

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


def parse_state_data(data):
    return {
        "model_overrides": {
            int(k): str(v) for k, v in data.get("model_overrides", {}).items()
        },
        "coder_chats": {int(x) for x in data.get("coder_chats", [])},
        "reasoning_hidden": {int(x) for x in data.get("reasoning_hidden", [])},
        "tools_hidden": {int(x) for x in data.get("tools_hidden", [])},
    }


def load_state_file(path):
    if not path.exists():
        return None
    try:
        return parse_state_data(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError, KeyError, TypeError, AttributeError):
        return None


_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()

_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY = 0.05


def _write_lock(path) -> threading.Lock:
    key = str(path)
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


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


def _write_text_atomic(path, text) -> bool:
    tmp_name = None
    try:
        with _write_lock(path):
            payload = _build_payload(text) if callable(text) else text
            handle, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
            )
            os.close(handle)
            with open(tmp_name, "w", encoding="utf-8") as stream:
                stream.write(payload)
            if path.is_file():
                os.chmod(tmp_name, stat.S_IMODE(path.stat().st_mode))
            _replace_with_retry(tmp_name, path)
            tmp_name = None
        return True
    except (OSError, TypeError, ValueError, RuntimeError):
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
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    return _write_text_atomic(path, snapshot)


def load_history_file(path, dm_limit, group_limit):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, KeyError, TypeError, AttributeError):
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

    def __init__(self, writer: Callable[[], None], delay=0.5, logger=None):
        self._writer = writer
        self._delay = delay
        self._logger = logger
        self._task = None
        self._dirty = False

    def mark_dirty(self):
        self._dirty = True
        if self._task is None or self._task.done():
            with contextlib.suppress(RuntimeError):
                self._task = asyncio.create_task(self._run())

    async def _write(self) -> bool:
        try:
            await asyncio.to_thread(self._writer)
        except SAVER_ERRORS as exc:
            if self._logger:
                self._logger.warning("AsyncSaver write failed: %r", exc)
            return False
        return True

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
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
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
            return self._ns[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in _MODE_KEYS:
            self._ns[name] = value
        else:
            object.__setattr__(self, name, value)


def load_state_into(store, state_file, logger=None):
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


def save_state_from(store, state_file, logger):
    if not save_state_file(
        state_file,
        {
            "model_overrides": store.model_overrides,
            "coder_chats": store.coder_chats,
            "reasoning_hidden": store.reasoning_hidden,
            "tools_hidden": store.tools_hidden,
        },
    ):
        logger.warning("Не удалось сохранить %s", state_file.name)


def load_history_into(store, history_file, dm_limit, group_limit, logger=None):
    history = load_history_file(history_file, dm_limit, group_limit)
    if history is None:
        if logger is not None and history_file.exists():
            logger.warning(
                "Не удалось прочитать %s, история сброшена", history_file.name
            )
        return
    store.chat_history = history


def save_history_from(store, history_file, logger):
    if not save_history_file(history_file, store.chat_history):
        logger.warning("Не удалось сохранить %s", history_file.name)


MAX_RECENT_IDS = 5000
MAX_SEEN_MSG_KEYS = 20000
_EVICT_BATCH = 1000


def _evict_oldest(target, limit: int) -> None:
    overflow = len(target) - limit
    if overflow <= 0:
        return
    for key in list(target)[: min(overflow, _EVICT_BATCH)]:
        target.discard(key)


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
            recent_ids.add((getattr(event, "chat_id", None), sent.id))
            _evict_oldest(recent_ids, MAX_RECENT_IDS)
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


def compose_prompt(prompt, replied_text, limit):
    if not replied_text:
        return prompt
    quoted = replied_text[: max(1, limit // 2)]
    if not prompt:
        return quoted
    header = QUOTE_HEADER.format(quoted=quoted)
    return header + prompt[: max(1, limit - len(header))]


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
            if last_chat_activity[k] < cutoff:
                del last_chat_activity[k]
    if cooldown > 0:
        last = last_chat_activity.get(chat_id)
        if last is not None and (now - last) < cooldown:
            return True
    last_chat_activity[chat_id] = now
    return False


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
) -> tuple[str, bool, bool] | None:
    cmd = command[0]
    if cmd == "clear":
        limit = dm_limit if is_private else group_limit
        hist = chat_history.setdefault(chat_id, deque(maxlen=limit))
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
    return None


async def append_group_history(chat_id, content, chat_history, group_limit, ctx_lock):
    async with ctx_lock:
        hist = chat_history.setdefault(chat_id, deque(maxlen=group_limit))
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
    store.seen_msg_keys.add(key)
    _evict_oldest(store.seen_msg_keys, MAX_SEEN_MSG_KEYS)
    stripped = strip_trigger_fn(text, triggered) or text.strip()
    if is_self:
        role = "user" if triggered else "assistant"
        content = strip_role_fn(stripped)
    else:
        role = "user"
        label = await sender_label_fn()
        content = f"{label}: {stripped}" if label else stripped
    limit = dm_limit if chat_id > 0 else group_limit
    async with store.ctx_lock:
        hist = store.chat_history.setdefault(chat_id, deque(maxlen=limit))
        hist.append({"role": role, "content": content})
    save_history_fn()


async def prepare_messages(store, chat_id, dm_limit, group_limit, system_fn):
    limit = dm_limit if chat_id > 0 else group_limit
    async with store.ctx_lock:
        hist = store.chat_history.setdefault(chat_id, deque(maxlen=limit))
        return [{"role": "system", "content": system_fn(chat_id)}, *list(hist)]


def trim_tool_history(messages, max_messages, head_size=1):
    if max_messages < 2 or len(messages) <= max_messages:
        return messages
    head = messages[:head_size]
    tail = messages[len(messages) - (max_messages - head_size) :]
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    if not tail or tail[0].get("role") != "assistant":
        return messages
    return [*head, *tail]


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
                    sanitize_tools=not unrestricted,
                    unrestricted=unrestricted,
                    stats=stats,
                )
        except caught as exc:
            error = exc
        full_answer = result or "".join(state["answer_parts"])
        final_text = _final_text(state, render, full_answer, error)
        if state["edit_id"] is not None:
            edited = await edit_fn(chat_id, state["edit_id"], final_text, logger=logger)
            delivered = bool(edited)
            if not edited and placeholder_id is not None:
                if logger is not None:
                    logger.warning(
                        "финальный edit не удался, отправляю ответ новым сообщением"
                    )
                delivered = bool(await reply_fn(event, final_text))
        elif not is_self:
            delivered = bool(await reply_fn(event, final_text))
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
        return final_text
    if full_answer.strip():
        return full_answer
    if state["reasoning_parts"] or state["tool_parts"]:
        return final_text
    if error is not None:
        return "Ошибка при обращении к DanyAPI. / DanyAPI request error."
    return "(пустой ответ / empty answer)"
