import ast
import asyncio
import contextlib
import datetime
import html
import ipaddress
import json
import logging
import math
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from telethon.errors import RPCError

import core

logger = logging.getLogger("danybot.tools")

PROJECT_DIR = Path(__file__).resolve().parent

SAFE_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
}

SAFE_CONSTS = {
    "pi": math.pi,
    "e": math.e,
}

CODER_ROOT = Path(os.getenv("CODER_ROOT", "/root")).expanduser().resolve()
MAX_READ_BYTES = 2_000_000
MAX_READ_OUTPUT = 60_000
MAX_WRITE_BYTES = 500_000
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_FILES = 4000
MAX_SEARCH_NODES = 40000
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_SEARCH_PATTERN = 250
SEARCH_TIMEOUT = 30
SEARCH_CHECK_EVERY = 200
MAX_SHELL_TIMEOUT = 300
MAX_SCRIPT_BYTES = 200_000
MAX_SCRIPT_OUTPUT = 4000
MAX_SHELL_OUTPUT = 4000
MAX_PROCESS_BYTES = 1_000_000
MAX_EVAL_EXPONENT = 1000
MAX_EVAL_STEPS = 5000
MAX_EVAL_BITS = 40000
MAX_FETCH_REDIRECTS = 3
MAX_FETCH_BYTES = 2_000_000
MAX_SEARCH_BYTES = 2_000_000
FETCH_TIMEOUT = 25

TOOL_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    AttributeError,
    RuntimeError,
    RecursionError,
    MemoryError,
    OverflowError,
    EOFError,
    sqlite3.Error,
    httpx.HTTPError,
    httpx.InvalidURL,
)


_SUBPROCESS_ENV_DENY = frozenset(
    {
        "API_ID",
        "API_HASH",
        "BOT_TOKEN",
        "DANYAPI_KEY",
        "OWNER_IDS",
        "SESSION_NAME",
        "VALIDATE_API_ID",
        "VALIDATE_API_HASH",
    }
)

_SUBPROCESS_ENV_HINTS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "APIKEY",
    "API_KEY",
    "_KEY",
    "KEY_",
    "HASH",
    "SESSION",
    "CREDENTIAL",
    "AUTH_SOCK",
    "AUTH_TOKEN",
    "COOKIE",
    "PRIVATE",
)


def _subprocess_env() -> dict[str, str]:
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _SUBPROCESS_ENV_DENY:
            continue
        if any(hint in upper for hint in _SUBPROCESS_ENV_HINTS):
            continue
        env[key] = value
    return env


def _resolve_path(raw):
    base = CODER_ROOT.resolve()
    candidate = Path(str(raw).strip() if raw else ".")
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = Path(os.path.normpath(str(candidate)))
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None, "Не удалось разрешить путь."
    if resolved != base and base not in resolved.parents:
        return None, f"Путь вне разрешённого корня: {base}"
    return resolved, ""


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


_EVAL_STEPS = 0


def _eval_guard() -> None:
    global _EVAL_STEPS
    _EVAL_STEPS += 1
    if _EVAL_STEPS > MAX_EVAL_STEPS:
        raise ValueError("Слишком сложное выражение")


def _eval_pow(left, right):
    if abs(right) > MAX_EVAL_EXPONENT:
        raise ValueError("Слишком большая степень")
    if (
        isinstance(left, int)
        and isinstance(right, int)
        and right > 0
        and left.bit_length() * right > MAX_EVAL_BITS
    ):
        raise ValueError("Слишком большое число")
    return left**right


SAFE_FUNCS["pow"] = _eval_pow


def _eval_node(node):
    _eval_guard()
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
            return _eval_pow(left, right)
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
        if node.id in SAFE_CONSTS:
            return SAFE_CONSTS[node.id]
        raise ValueError("Недопустимое имя")
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in SAFE_FUNCS:
            if node.keywords:
                raise ValueError("Именованные аргументы не поддерживаются")
            args = [_eval_node(a) for a in node.args]
            return SAFE_FUNCS[node.func.id](*args)
        raise ValueError("Недопустимый вызов")
    raise ValueError("Недопустимая конструкция")


def safe_eval(expression: str) -> str:
    global _EVAL_STEPS
    _EVAL_STEPS = 0
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
        RecursionError,
        MemoryError,
    ) as exc:
        return f"Ошибка вычисления: {exc}"


MAX_OPT_INT = 1_000_000


def _int_arg(arguments, key, default, lo, hi):
    try:
        value = int(arguments.get(key, default))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(lo, min(value, hi))


def _str_arg(arguments, key, default=""):
    return str(arguments.get(key, default)).strip()


def _too_big(path):
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size > MAX_READ_BYTES:
        return f"Файл слишком большой: {size} байт, максимум {MAX_READ_BYTES}."
    return ""


def _opt_int_arg(arguments, key, lo, hi):
    if arguments.get(key) is None:
        return None
    if hi is None:
        return _int_arg(arguments, key, lo, lo, MAX_OPT_INT)
    return _int_arg(arguments, key, lo, lo, hi)


MAX_RENDER_CHARS = 4000
MAX_PREFIX_CHARS = 800
REASONING_CHARS = 3000
ANSWER_CHARS = 3000


def render_response(
    prefix,
    reasoning_parts,
    tool_parts,
    full_answer,
    show_reasoning=True,
    show_tools=True,
):
    prefix = (
        prefix if len(prefix) <= MAX_PREFIX_CHARS else prefix[:MAX_PREFIX_CHARS] + "…"
    )
    reasoning = (
        _clip("".join(reasoning_parts), REASONING_CHARS) if show_reasoning else ""
    )
    answer = _clip(full_answer, ANSWER_CHARS)
    parts = []
    if reasoning:
        parts.append(f"reasoning:\n{reasoning}")
    if show_tools:
        seen = []
        marked = set()
        for tool in tool_parts:
            if tool in marked:
                continue
            marked.add(tool)
            seen.append(tool)
        if seen:
            parts.append("tools: " + ", ".join(seen))
    if answer:
        parts.append(answer)
    text = prefix + "\n\n".join(parts)
    if len(text) > MAX_RENDER_CHARS:
        droppable = list(range(len(parts) - 1))
        combos: list[tuple[int, ...]] = [()]
        combos.extend((i,) for i in droppable)
        if len(droppable) > 1:
            combos.append(tuple(droppable))
        for combo in combos:
            kept = [p for i, p in enumerate(parts) if i not in combo]
            candidate = prefix + "\n\n".join(kept)
            if len(candidate) <= MAX_RENDER_CHARS:
                text = candidate
                break
    if len(text) > MAX_RENDER_CHARS:
        text = text[: MAX_RENDER_CHARS - 1] + "…"
    return text


TOOLS: list[dict[str, Any]] = [
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
            "name": "get_profile",
            "description": "Информация о своём аккаунте",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Выполнить команду в shell и вернуть stdout/stderr",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
                    "cwd": {
                        "type": "string",
                        "description": "Рабочий каталог внутри разрешённого корня",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Найти информацию в интернете по запросу",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Скачать содержимое URL и вернуть текст",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 100, "maximum": 20000},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Текущие дата и время (UTC и локальное)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "text_stats",
            "description": "Статистика текста: символы, слова, строки",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bot_stats",
            "description": "Статистика бота: аптайм, контекст, модель",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_remember",
            "description": (
                "Сохранить факт в долговременную память: ключ, значение, теги"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": (
                "Прочитать из памяти: по ключу, по подстроке (query) или последние"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_forget",
            "description": "Удалить запись из памяти по ключу или id",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": "Список последних записей памяти и статистика",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_skill",
            "description": ("Сохранить или обновить скилл: имя, описание, тело, теги"),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "body": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["name", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Загрузить скилл по имени и увеличить счётчик использования",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "Список скиллов с фильтром по тегу или подстроке",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_skill",
            "description": "Удалить скилл по имени",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_subagent",
            "description": (
                "Запустить одного или нескольких универсальных субагентов. "
                "Субагенты работают автономно и параллельно, каждый со своим "
                "набором инструментов. По умолчанию доступны чтение, поиск, "
                "время, статистика, веб, память и скиллы. В кодер-режиме "
                "владельца субагент наследует инструменты сессии, кроме "
                "run_subagent. Вернуть результаты всех задач."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Одна задача для субагента",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Список задач для параллельного запуска",
                    },
                    "system": {
                        "type": "string",
                        "description": "Системная инструкция субагента",
                    },
                    "model": {"type": "string"},
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Разрешённые инструменты (по умолчанию все)",
                    },
                    "concurrency": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 16,
                    },
                    "max_rounds": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Сколько раундов инструментов разрешить. "
                            "Без значения раундов не ограничено."
                        ),
                    },
                },
            },
        },
    },
]

FILE_TOOL_NAMES = (
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "search_files",
    "execute_script",
)

FILE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать текстовый файл с нумерацией строк",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Записать файл целиком, создав каталоги при необходимости",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Заменить фрагмент текста в файле",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "Показать содержимое каталога",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Найти строки по регулярному выражению в файлах",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_script",
            "description": (
                "Выполнить python-скрипт целиком и вернуть rc, stdout и stderr"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
                    "cwd": {
                        "type": "string",
                        "description": "Рабочий каталог внутри разрешённого корня",
                    },
                },
                "required": ["code"],
            },
        },
    },
]

MEMORY_TOOL_NAMES = (
    "memory_remember",
    "memory_recall",
    "memory_forget",
    "memory_list",
    "save_skill",
    "load_skill",
    "list_skills",
    "delete_skill",
)

CODER_TOOL_NAMES = (
    *FILE_TOOL_NAMES,
    *MEMORY_TOOL_NAMES,
    "run_shell",
    "web_search",
    "fetch_url",
    "get_time",
)

CODER_TOOLS: list[dict[str, Any]] = [
    item
    for item in [*FILE_TOOLS, *TOOLS]
    if item["function"]["name"] in CODER_TOOL_NAMES
]

BOT_TOOLS: list[dict[str, Any]] = list(TOOLS)

OWNER_ONLY_TOOLS = frozenset(
    {
        "run_shell",
        "execute_script",
        "run_subagent",
        "memory_remember",
        "memory_forget",
        "save_skill",
        "delete_skill",
        *FILE_TOOL_NAMES,
    }
)

PUBLIC_TOOLS: list[dict[str, Any]] = [
    item
    for item in [*FILE_TOOLS, *TOOLS]
    if item["function"]["name"] not in OWNER_ONLY_TOOLS
]

SUBAGENT_EXCLUDED_TOOLS = frozenset(
    {"run_subagent", "run_shell", *FILE_TOOL_NAMES, *MEMORY_TOOL_NAMES}
)

SUBAGENT_EXCLUDED_UNRESTRICTED = frozenset({"run_subagent"})


def _entity_json(entity) -> str:
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    return json.dumps(
        {
            "id": getattr(entity, "id", None),
            "name": " ".join(x for x in [first, last] if x).strip(),
            "username": getattr(entity, "username", None),
        },
        ensure_ascii=False,
    )


async def _tool_evaluate(arguments, chat_id, client, stats, unrestricted=False):
    return safe_eval(_str_arg(arguments, "expression"))


async def _tool_get_chat_info(arguments, chat_id, client, stats, unrestricted=False):
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


async def _tool_get_user_info(arguments, chat_id, client, stats, unrestricted=False):
    handle = _str_arg(arguments, "handle")
    if not handle:
        return "Пустой handle."
    try:
        entity = await client.get_entity(handle)
    except (RPCError, OSError, ValueError) as exc:
        return f"Ошибка получения пользователя: {exc}"
    return _entity_json(entity)


async def _tool_get_profile(arguments, chat_id, client, stats, unrestricted=False):
    try:
        me = await client.get_me()
    except (RPCError, OSError, ValueError) as exc:
        return f"Ошибка получения профиля: {exc}"
    return _entity_json(me)


def _kill_process_now(proc) -> None:
    if proc.returncode is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
                check=False,
            )
    else:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(-proc.pid, getattr(signal, "SIGKILL", 9))
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


async def _kill_process(proc) -> None:
    _kill_process_now(proc)
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await asyncio.wait_for(proc.wait(), timeout=5)


def _spawn_kwargs():
    if os.name == "nt":
        return {}
    return {"start_new_session": True}


class _OutputSink:
    def __init__(self, cap: int):
        self.cap = cap
        self.size = 0
        self.truncated = False
        self.parts: list[bytes] = []

    def feed(self, chunk: bytes) -> None:
        if self.truncated:
            return
        room = self.cap - self.size
        if len(chunk) > room:
            self.parts.append(chunk[:room])
            self.size = self.cap
            self.truncated = True
            return
        self.parts.append(chunk)
        self.size += len(chunk)

    def text(self) -> str:
        return b"".join(self.parts).decode("utf-8", errors="replace")


async def _drain(reader, sink: _OutputSink) -> None:
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            return
        sink.feed(chunk)


def _exit_code(proc) -> int:
    return proc.returncode if proc.returncode is not None else -1


DRAIN_GRACE = 5


def _close_pipes(proc) -> None:
    close = getattr(getattr(proc, "_transport", None), "close", None)
    if close is None:
        return
    with contextlib.suppress(Exception):
        close()


async def _collect_process(proc, timeout: int, cap: int) -> tuple[int, str, str, str]:
    out = _OutputSink(cap)
    err = _OutputSink(cap)
    drain = asyncio.gather(_drain(proc.stdout, out), _drain(proc.stderr, err))
    try:
        done, _pending = await asyncio.wait({drain}, timeout=timeout)
        if not done:
            await _kill_process(proc)
            _close_pipes(proc)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(drain, timeout=DRAIN_GRACE)
            return -1, out.text(), err.text(), f"Таймаут {timeout}s: команда прервана."
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        note = ""
        if proc.returncode is None:
            note = "Процесс не завершился после закрытия вывода, убит."
        if out.truncated or err.truncated:
            note = f"{note} Вывод обрезан.".strip()
        return _exit_code(proc), out.text(), err.text(), note
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await _kill_process(proc)
        if not drain.done():
            drain.cancel()
        with contextlib.suppress(BaseException):
            await drain


def _format_process_result(rc: int, out: str, err: str) -> str:
    result = f"rc={rc}\nstdout:\n{out}"
    if err:
        result += f"\nstderr:\n{err}"
    return result


def _clip_process_result(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (вывод обрезан, {len(text)} символов)"


def _resolve_workdir(raw_cwd):
    if not raw_cwd:
        return CODER_ROOT, ""
    workdir, err = _resolve_path(raw_cwd)
    if err or workdir is None:
        return None, err
    if not workdir.is_dir():
        return None, f"Каталог не найден: {workdir}"
    return workdir, ""


async def _tool_run_shell(arguments, chat_id, client, stats, unrestricted=False):
    command = _str_arg(arguments, "command")
    if not command:
        return "Пустая команда."
    timeout = _int_arg(arguments, "timeout", 30, 1, MAX_SHELL_TIMEOUT)
    workdir, err = _resolve_workdir(_str_arg(arguments, "cwd"))
    if err:
        return err
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir),
            env=_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_spawn_kwargs(),
        )
    except OSError as exc:
        return f"Ошибка запуска: {exc}"
    rc, out, err_out, note = await _collect_process(proc, timeout, MAX_PROCESS_BYTES)
    result = _clip_process_result(
        _format_process_result(rc, out, err_out), MAX_SHELL_OUTPUT
    )
    return f"{result}\n{note}" if note else result


_httpx_singleton = None
_httpx_loop = None
_httpx_closing: set[Any] = set()


def _build_httpx_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=30.0),
    )


def _get_httpx_client():
    global _httpx_singleton, _httpx_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _httpx_singleton is not None and not _httpx_singleton.is_closed:
        if loop is _httpx_loop:
            return _httpx_singleton
        stale, stale_loop = _httpx_singleton, _httpx_loop
        _httpx_singleton = None
        if stale_loop is not None and stale_loop.is_running():
            with contextlib.suppress(RuntimeError):
                closing = stale_loop.create_task(stale.aclose())
                _httpx_closing.add(closing)
                closing.add_done_callback(_httpx_closing.discard)
    _httpx_loop = loop
    _httpx_singleton = _build_httpx_client()
    return _httpx_singleton


async def close_httpx_client() -> None:
    global _httpx_singleton, _httpx_loop
    client = _httpx_singleton
    _httpx_singleton = None
    _httpx_loop = None
    if client is None:
        return
    with contextlib.suppress(Exception):
        await client.aclose()


BRAVE_SEARCH_URL = "https://search.brave.com/search"
DDG_SEARCH_URL = "https://duckduckgo.com/html/"

_BRAVE_START = re.compile(r'<div class="snippet[^"]*"[^>]*data-type="web"')
_BRAVE_HREF = re.compile(r'<a href="(https?://[^"]+)"')
_BRAVE_TITLE = re.compile(r'class="title[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_BRAVE_DESC = re.compile(r'class="generic-snippet[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_DDG_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)


def _strip_tags(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _brave_blocks(text):
    starts = [m.start() for m in _BRAVE_START.finditer(text)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        yield text[start:end]


def _parse_brave(text, limit):
    results = []
    for block in _brave_blocks(text):
        match = _BRAVE_HREF.search(block)
        if not match:
            continue
        href = match.group(1)
        title = _BRAVE_TITLE.search(block)
        desc = _BRAVE_DESC.search(block)
        lines = [_strip_tags(title.group(1)) if title else "", href]
        if desc:
            lines.append(_strip_tags(desc.group(1)))
        chunk = "\n".join(line for line in lines if line)
        if chunk:
            results.append(chunk)
        if len(results) >= limit:
            break
    return results


def _parse_ddg(text, limit):
    results = []
    for href, title in _DDG_RESULT.findall(text):
        clean = _strip_tags(title)
        if "uddg=" in href:
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            href = parsed.get("uddg", [href])[0]
        results.append(f"{clean}\n{href}")
        if len(results) >= limit:
            break
    return results


SEARCH_ENGINES = ((BRAVE_SEARCH_URL, _parse_brave), (DDG_SEARCH_URL, _parse_ddg))


def _error_label(exc) -> str:
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code is not None:
        return f"HTTP {code}"
    return type(exc).__name__


async def _tool_web_search(arguments, chat_id, client, stats, unrestricted=False):
    query = _str_arg(arguments, "query")
    if not query:
        return "Пустой запрос."
    limit = _int_arg(arguments, "limit", 5, 1, 10)
    hc = _get_httpx_client()
    errors = []
    for url, parser in SEARCH_ENGINES:
        try:
            async with hc.stream("GET", url, params={"q": query}, timeout=20) as resp:
                resp.raise_for_status()
                body, _cut = await _read_capped(resp, MAX_SEARCH_BYTES)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            errors.append(f"{url}: {_error_label(exc)}")
            continue
        results = parser(body.decode("utf-8", errors="replace"), limit)
        if results:
            return "\n\n".join(results)
        errors.append(f"{url}: пустая выдача")
    logger.warning("web_search без результатов: %s", "; ".join(errors))
    failures = [e for e in errors if "пустая выдача" not in e]
    if failures:
        return f"Ошибка поиска: {'; '.join(failures)}"
    return "Ничего не найдено."


async def _resolve_addrs(host, port):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
IPV4_COMPAT_PREFIX = ipaddress.IPv6Network("::/96")


def _is_public_addr(addr) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped
        if embedded is None:
            embedded = ip.sixtofour
        if embedded is None and ip.teredo is not None:
            embedded = ip.teredo[1]
        if embedded is None:
            if ip in NAT64_PREFIX or ip in IPV4_COMPAT_PREFIX:
                return False
        else:
            ip = embedded
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_loopback
    )


async def _check_public_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        return f"Некорректный URL: {exc}"
    if parsed.scheme not in ("http", "https"):
        return f"Схема заблокирована: {parsed.scheme or 'пусто'}"
    host = parsed.hostname
    if not host:
        return "URL без хоста."
    try:
        port = parsed.port
    except ValueError as exc:
        return f"Некорректный порт в URL: {exc}"
    port = port or (443 if parsed.scheme == "https" else 80)
    try:
        addrs = await _resolve_addrs(host, port)
    except (OSError, ValueError, UnicodeError) as exc:
        return f"Не удалось разрешить хост {host}: {exc}"
    if not addrs:
        return f"Не удалось разрешить хост {host}."
    for addr in addrs:
        if not _is_public_addr(addr):
            return f"Адрес заблокирован: {host} -> {addr}"
    return ""


async def _tool_fetch_url(arguments, chat_id, client, stats, unrestricted=False):
    url = _str_arg(arguments, "url")
    if not url:
        return "Пустой URL."
    if not url.startswith(("http://", "https://")):
        return "URL должен начинаться с http:// или https://"
    max_chars = _int_arg(arguments, "max_chars", 5000, 100, 20000)
    hc = _get_httpx_client()
    current = url
    try:
        async with asyncio.timeout(FETCH_TIMEOUT + 5):
            for _hop in range(MAX_FETCH_REDIRECTS + 1):
                guard = await _check_public_url(current)
                if guard:
                    return guard
                try:
                    status, location, raw, truncated = await _fetch_once(hc, current)
                except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
                    return f"Ошибка загрузки: {exc}"
                if 300 <= status < 400 and location:
                    try:
                        current = urllib.parse.urljoin(current, location)
                    except ValueError as exc:
                        return f"Некорректный редирект: {exc}"
                    continue
                break
            else:
                return "Слишком много перенаправлений."
    except TimeoutError:
        return f"Таймаут {FETCH_TIMEOUT + 5}s: загрузка прервана."
    page = raw.decode("utf-8", errors="replace")
    cleaned = _strip_page(page)
    text = cleaned[:max_chars]
    if truncated:
        text += "\n… (загрузка обрезана)"
    return text


MAX_TAG_BYTES = 2000
_SKIP_TAGS = ("script", "style")


def _strip_page(raw: str) -> str:
    out: list[str] = []
    size = len(raw)
    pos = 0
    skip_name = ""
    while pos < size:
        lt = raw.find("<", pos)
        if lt < 0:
            break
        limit = min(size, lt + MAX_TAG_BYTES)
        gt = lt + 1
        while gt < limit and raw[gt] != ">":
            gt += 1
        if gt >= limit:
            if not skip_name:
                out.append(raw[pos:lt])
            pos = limit
            continue
        tag = raw[lt + 1 : gt]
        closing = tag.startswith("/")
        if closing:
            tag = tag[1:]
        cut = 0
        while cut < len(tag) and (tag[cut].isalnum() or tag[cut] in "-_:."):
            cut += 1
        name = tag[:cut].lower()
        if not skip_name:
            out.append(raw[pos:lt])
        if name in _SKIP_TAGS:
            if closing:
                if name == skip_name:
                    skip_name = ""
            elif not skip_name:
                skip_name = name
        pos = gt + 1
    if not skip_name:
        out.append(raw[pos:])
    return re.sub(r"\s+", " ", html.unescape(" ".join(out))).strip()


async def _read_capped(resp, cap: int) -> tuple[bytes, bool]:
    buf = bytearray()
    truncated = False
    async for chunk in resp.aiter_bytes():
        room = cap - len(buf)
        if room <= 0:
            truncated = True
            break
        if len(chunk) > room:
            buf.extend(chunk[:room])
            truncated = True
            break
        buf.extend(chunk)
    return bytes(buf), truncated


async def _fetch_once(hc, url: str) -> tuple[int, str, bytes, bool]:
    async with hc.stream("GET", url, follow_redirects=False, timeout=20) as resp:
        resp.raise_for_status()
        status = int(getattr(resp, "status_code", 200) or 200)
        location = (getattr(resp, "headers", {}) or {}).get("location", "") or ""
        if 300 <= status < 400 and location:
            return status, str(location), b"", False
        body, truncated = await _read_capped(resp, MAX_FETCH_BYTES)
        return status, str(location), body, truncated


WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


async def _tool_get_time(arguments, chat_id, client, stats, unrestricted=False):
    utc = datetime.datetime.now(datetime.timezone.utc)
    return json.dumps(
        {
            "utc": utc.isoformat(),
            "local": utc.astimezone().isoformat(),
            "weekday": WEEKDAYS[utc.weekday()],
        },
        ensure_ascii=False,
    )


async def _tool_text_stats(arguments, chat_id, client, stats, unrestricted=False):
    text = str(arguments.get("text", ""))
    return json.dumps(
        {
            "chars": len(text),
            "chars_no_spaces": len(re.sub(r"\s", "", text)),
            "words": len(text.split()),
            "lines": len(text.splitlines()) or 1,
        },
        ensure_ascii=False,
    )


async def _tool_get_bot_stats(arguments, chat_id, client, stats, unrestricted=False):
    data = stats(chat_id) if stats is not None else {}
    return json.dumps(data, ensure_ascii=False)


SUBAGENT_RESULT_CHARS = 1500
SUBAGENT_REPORT_CHARS = 8000
SUBAGENT_NAME_CHARS = 60
SUBAGENT_TASK_CHARS = 300


def _tool_name_list(raw) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    return [str(item).strip() for item in raw if str(item).strip()] or None


def _report_item(item, result_chars, tool_chars) -> dict[str, Any]:
    used = [str(name) for name in (item.get("tools_used") or [])][: max(0, tool_chars)]
    return {
        "name": str(item.get("name", "universal"))[:SUBAGENT_NAME_CHARS],
        "task": str(item.get("task", ""))[:SUBAGENT_TASK_CHARS],
        "ok": bool(item.get("ok")),
        "rounds": item.get("rounds", 0),
        "tools_used": used,
        "result": str(item.get("result", ""))[:result_chars],
    }


def _subagent_report(results) -> str:
    items = list(results)
    for result_chars, tool_chars in (
        (SUBAGENT_RESULT_CHARS, 60),
        (400, 30),
        (100, 10),
        (0, 0),
    ):
        payload = [_report_item(item, result_chars, tool_chars) for item in items]
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) <= SUBAGENT_REPORT_CHARS:
            return text
    while len(items) > 1:
        items = items[:-1]
        text = json.dumps(
            [_report_item(item, 0, 0) for item in items], ensure_ascii=False
        )
        if len(text) <= SUBAGENT_REPORT_CHARS:
            return text
    return json.dumps([_report_item(items[0], 0, 0)], ensure_ascii=False)


async def _tool_run_subagent(arguments, chat_id, client, stats, unrestricted=False):
    import subagents

    if not subagents.is_configured():
        return "Субагенты недоступны."
    tasks = arguments.get("tasks")
    if tasks is None:
        single = _str_arg(arguments, "task")
        tasks = [single] if single else []
    elif isinstance(tasks, str):
        tasks = [tasks] if tasks.strip() else []
    elif not isinstance(tasks, (list, tuple)):
        return "Поле tasks должно быть списком строк."
    else:
        tasks = [str(item).strip() for item in tasks if str(item).strip()]
    if not tasks:
        return "Нужна задача: task или tasks."
    results = await subagents.run_subagents(
        tasks,
        concurrency=_opt_int_arg(arguments, "concurrency", 1, 16),
        system=_str_arg(arguments, "system") or None,
        model=_str_arg(arguments, "model") or None,
        tool_names=_tool_name_list(arguments.get("tools")),
        chat_id=chat_id,
        client=client,
        max_rounds=_opt_int_arg(arguments, "max_rounds", 1, None),
        verify=not unrestricted,
        stats=stats,
        unrestricted=unrestricted,
    )
    return _subagent_report(results)


async def _tool_read_file(arguments, chat_id, client, stats, unrestricted=False):
    path, err = _resolve_path(arguments.get("path"))
    if err or path is None:
        return err
    if not path.exists():
        return f"Файл не найден: {path}"
    if path.is_dir():
        return f"Это каталог: {path}. Используй list_dir."
    too_big = _too_big(path)
    if too_big:
        return too_big
    offset = _int_arg(arguments, "offset", 1, 1, 10**9)
    limit = _int_arg(arguments, "limit", 400, 1, 5000)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        return f"Ошибка чтения: {exc}"
    lines = raw.splitlines()
    total = len(lines)
    start = min(offset, total + 1)
    chunk = lines[start - 1 : start - 1 + limit]
    head = f"{path} | строк {total} | показано {len(chunk)} с {start}"
    if not chunk:
        return f"{head}\n(пусто)"
    body = "\n".join(f"{start + i}|{line}" for i, line in enumerate(chunk))
    return f"{head}\n{_clip(body, MAX_READ_OUTPUT)}"


async def _tool_write_file(arguments, chat_id, client, stats, unrestricted=False):
    path, err = _resolve_path(arguments.get("path"))
    if err or path is None:
        return err
    content = str(arguments.get("content", ""))
    size = len(content.encode("utf-8"))
    if size > MAX_WRITE_BYTES:
        return f"Слишком большой объём: {size} байт"
    if path.is_dir():
        return f"Это каталог: {path}"
    existed = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Ошибка записи: {exc}"
    if not core._write_text_atomic(path, content):
        return f"Ошибка записи: {path}"
    action = "Перезаписан" if existed else "Создан"
    return f"{action}: {path} ({len(content)} символов)"


async def _tool_edit_file(arguments, chat_id, client, stats, unrestricted=False):
    path, err = _resolve_path(arguments.get("path"))
    if err or path is None:
        return err
    if not path.is_file():
        return f"Файл не найден: {path}"
    too_big = _too_big(path)
    if too_big:
        return too_big
    old = str(arguments.get("old_string", ""))
    new = str(arguments.get("new_string", ""))
    if not old:
        return "Пустой old_string."
    if old == new:
        return "old_string и new_string совпадают."
    replace_all = bool(arguments.get("replace_all", False))
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return f"Ошибка чтения: {exc}"
    count = raw.count(old)
    if count == 0:
        return "Фрагмент не найден."
    if count > 1 and not replace_all:
        return f"Фрагмент встречается {count} раз, уточни old_string или replace_all."
    updated = raw.replace(old, new) if replace_all else raw.replace(old, new, 1)
    size = len(updated.encode("utf-8"))
    if size > MAX_WRITE_BYTES:
        return f"Слишком большой объём: {size} байт"
    if not core._write_text_atomic(path, updated):
        return f"Ошибка записи: {path}"
    return f"Изменён: {path} (замен {count if replace_all else 1}, {size} байт)"


def _list_entries(path: Path) -> tuple[list[Path], int]:
    entries: list[Path] = []
    total = 0
    with os.scandir(path) as scan:
        for item in scan:
            total += 1
            if len(entries) < MAX_LIST_ENTRIES:
                entries.append(Path(item.path))
    entries.sort(key=lambda p: (p.is_file(), p.name.lower()))
    return entries, total


async def _tool_list_dir(arguments, chat_id, client, stats, unrestricted=False):
    path, err = _resolve_path(arguments.get("path", "."))
    if err or path is None:
        return err
    if not path.exists():
        return f"Каталог не найден: {path}"
    if path.is_file():
        return f"Это файл: {path}. Используй read_file."
    try:
        entries, total = await asyncio.to_thread(_list_entries, path)
    except OSError as exc:
        return f"Ошибка чтения каталога: {exc}"
    rows = []
    for entry in entries:
        try:
            rows.append(
                f"{entry.name}/"
                if entry.is_dir()
                else f"{entry.name} ({entry.stat().st_size})"
            )
        except OSError:
            rows.append(entry.name)
    head = f"{path} | элементов {total}"
    if total > len(rows):
        head += f", показано {len(rows)}"
    if not rows:
        return f"{head}\n(пусто)"
    return head + "\n" + "\n".join(rows)


def _is_safe_glob(pattern: str) -> bool:
    if pattern.startswith(("/", "\\", "~")) or re.match(r"^[a-zA-Z]:", pattern):
        return False
    return ".." not in re.split(r"[\\/]+", pattern)


def _scan_files(
    root: Path, glob_pat: str, rx, limit: int, stop
) -> tuple[list[str], str]:
    matches: list[str] = []
    scanned = 0
    visited = 0
    skipped_big = 0
    note = ""
    candidates = [root] if root.is_file() else root.rglob(glob_pat)
    for item in candidates:
        if stop.is_set():
            return matches, note or "остановлено по таймауту"
        if len(matches) >= limit:
            note = f"достигнут лимит результатов {limit}"
            break
        if scanned >= MAX_SEARCH_FILES:
            note = f"просмотрено не больше {MAX_SEARCH_FILES} файлов"
            break
        if visited >= MAX_SEARCH_NODES:
            note = f"обойдено не больше {MAX_SEARCH_NODES} элементов"
            break
        visited += 1
        if not item.is_file():
            continue
        try:
            real = item.resolve()
        except (OSError, RuntimeError):
            continue
        if real != root and root not in real.parents:
            continue
        try:
            if item.stat().st_size > MAX_SEARCH_FILE_BYTES:
                skipped_big += 1
                continue
            scanned += 1
            text = item.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if idx % SEARCH_CHECK_EVERY == 0 and stop.is_set():
                return matches, note or "остановлено по таймауту"
            if rx.search(line):
                matches.append(f"{item}:{idx}: {line.strip()[:200]}")
                if len(matches) >= limit:
                    break
    if skipped_big and not note:
        note = f"пропущено файлов больше {MAX_SEARCH_FILE_BYTES} байт: {skipped_big}"
    return matches, note


SCAN_LIMIT_KEYS = (
    "MAX_SEARCH_NODES",
    "MAX_SEARCH_FILES",
    "MAX_SEARCH_FILE_BYTES",
)

SCAN_ERRORS: tuple[type[BaseException], ...] = (*TOOL_ERRORS, re.error)


def _reconfigure_stream(stream) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    with contextlib.suppress(ValueError, OSError):
        reconfigure(encoding="utf-8")


def _scan_worker_main() -> None:
    for stream in (sys.stdin, sys.stdout):
        _reconfigure_stream(stream)
    allowed_env = _subprocess_env()
    for name in [key for key in os.environ if key not in allowed_env]:
        del os.environ[name]
    os.environ.update(allowed_env)
    request = json.loads(sys.stdin.read())
    for name, value in (request.get("limits") or {}).items():
        if name in SCAN_LIMIT_KEYS:
            setattr(sys.modules[__name__], name, value)
    stop = threading.Event()
    timer = threading.Timer(max(1.0, SEARCH_TIMEOUT - 5.0), stop.set)
    timer.daemon = True
    timer.start()
    try:
        matches, note = _scan_files(
            Path(request["root"]),
            request["glob"],
            re.compile(request["pattern"]),
            int(request["limit"]),
            stop,
        )
    except SCAN_ERRORS as exc:
        payload = {"matches": [], "note": "", "error": repr(exc)}
    else:
        payload = {"matches": matches, "note": note, "error": ""}
    finally:
        timer.cancel()
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


async def _run_search_subprocess(root, glob_pat, pattern, limit) -> tuple[list, str]:
    request = json.dumps(
        {
            "root": str(root),
            "glob": glob_pat,
            "pattern": pattern,
            "limit": limit,
            "limits": {name: globals()[name] for name in SCAN_LIMIT_KEYS},
        },
        ensure_ascii=False,
    )
    env = _subprocess_env()
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PROJECT_DIR), existing_path) if part
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import tools; tools._scan_worker_main()",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(PROJECT_DIR),
        env=env,
        **_spawn_kwargs(),
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(request.encode("utf-8")), timeout=SEARCH_TIMEOUT
        )
    except TimeoutError:
        await _kill_process(proc)
        _close_pipes(proc)
        return [], f"Таймаут поиска: {SEARCH_TIMEOUT}s"
    if proc.returncode != 0:
        detail = err.decode("utf-8", errors="replace").strip()[-200:]
        return [], f"сканер поиска не отработал: {detail}"
    try:
        payload = json.loads(out.decode("utf-8", errors="replace") or "{}")
    except ValueError as exc:
        return [], f"сканер поиска вернул мусор: {exc}"
    if payload.get("error"):
        return [], f"сканер поиска упал: {payload['error']}"
    return list(payload.get("matches") or []), str(payload.get("note") or "")


async def _tool_search_files(arguments, chat_id, client, stats, unrestricted=False):
    pattern = _str_arg(arguments, "pattern")
    if not pattern:
        return "Пустой pattern."
    if len(pattern) > MAX_SEARCH_PATTERN:
        return f"Слишком длинный pattern: максимум {MAX_SEARCH_PATTERN} символов."
    path, err = _resolve_path(arguments.get("path", "."))
    if err or path is None:
        return err
    if not path.exists():
        return f"Путь не найден: {path}"
    glob_pat = _str_arg(arguments, "glob") or "*"
    if not _is_safe_glob(glob_pat):
        return "Недопустимый glob."
    limit = _int_arg(arguments, "limit", 60, 1, MAX_SEARCH_RESULTS)
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"Некорректное выражение: {exc}"
    try:
        matches, note = await _run_search_subprocess(path, glob_pat, pattern, limit)
    except OSError as exc:
        return f"Ошибка запуска сканера: {exc}"
    text = "\n".join(matches) if matches else "Совпадений не найдено."
    if note:
        text = f"{text}\n\nПоиск неполный: {note}. Сузить path, glob или pattern."
    return text


async def _tool_execute_script(arguments, chat_id, client, stats, unrestricted=False):
    code = str(arguments.get("code", ""))
    if not code.strip():
        return "Пустой код."
    encoded = code.encode("utf-8")
    if len(encoded) > MAX_SCRIPT_BYTES:
        return f"Слишком большой объём: {len(encoded)} байт"
    timeout = _int_arg(arguments, "timeout", 30, 1, MAX_SHELL_TIMEOUT)
    workdir, err = _resolve_workdir(_str_arg(arguments, "cwd"))
    if err:
        return err
    script_path = None
    proc = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as handle:
            script_path = Path(handle.name)
            handle.write(code)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script_path),
            cwd=str(workdir),
            env=_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_spawn_kwargs(),
        )
    except OSError as exc:
        return f"Ошибка запуска: {exc}"
    finally:
        if proc is None and script_path is not None:
            with contextlib.suppress(OSError):
                script_path.unlink()
    try:
        rc, out, err_out, note = await _collect_process(
            proc, timeout, MAX_PROCESS_BYTES
        )
    finally:
        if script_path is not None:
            with contextlib.suppress(OSError):
                script_path.unlink()
    result = _clip_process_result(
        _format_process_result(rc, out, err_out), MAX_SCRIPT_OUTPUT
    )
    return f"{result}\n{note}" if note else result


async def _tool_memory_remember(arguments, chat_id, client, stats, unrestricted=False):
    import memory

    key = _str_arg(arguments, "key")
    value = str(arguments.get("value", ""))
    tags = arguments.get("tags")
    result = await asyncio.to_thread(memory.remember, chat_id, key, value, tags)
    return memory.dumps(result)


async def _tool_memory_recall(arguments, chat_id, client, stats, unrestricted=False):
    import memory

    key = _str_arg(arguments, "key")
    query = _str_arg(arguments, "query")
    limit = _int_arg(arguments, "limit", 10, 1, 200)
    result = await asyncio.to_thread(
        memory.recall, chat_id, key=key, query=query, limit=limit
    )
    return memory.dumps(result)


async def _tool_memory_forget(arguments, chat_id, client, stats, unrestricted=False):
    import memory

    key = _str_arg(arguments, "key")
    mem_id = _int_arg(arguments, "id", 0, 0, 10**9)
    result = await asyncio.to_thread(memory.forget, chat_id, key=key, mem_id=mem_id)
    return memory.dumps(result)


async def _tool_memory_list(arguments, chat_id, client, stats, unrestricted=False):
    import memory

    limit = _int_arg(arguments, "limit", 50, 1, 200)
    result = await asyncio.to_thread(memory.list_memories, chat_id, limit=limit)
    result["stats"] = await asyncio.to_thread(memory.stats, chat_id)
    return memory.dumps(result)


async def _tool_save_skill(arguments, chat_id, client, stats, unrestricted=False):
    import skills

    name = _str_arg(arguments, "name")
    description = _str_arg(arguments, "description")
    body = str(arguments.get("body", ""))
    tags = arguments.get("tags")
    result = await asyncio.to_thread(skills.save_skill, name, description, body, tags)
    return skills.dumps(result)


async def _tool_load_skill(arguments, chat_id, client, stats, unrestricted=False):
    import skills

    name = _str_arg(arguments, "name")
    result = await asyncio.to_thread(skills.load_skill, name)
    return skills.dumps(result)


async def _tool_list_skills(arguments, chat_id, client, stats, unrestricted=False):
    import skills

    tag = _str_arg(arguments, "tag")
    query = _str_arg(arguments, "query")
    limit = _int_arg(arguments, "limit", 50, 1, 200)
    result = await asyncio.to_thread(
        skills.list_skills, tag=tag, query=query, limit=limit
    )
    result["stats"] = await asyncio.to_thread(skills.stats)
    return skills.dumps(result)


async def _tool_delete_skill(arguments, chat_id, client, stats, unrestricted=False):
    import skills

    name = _str_arg(arguments, "name")
    result = await asyncio.to_thread(skills.delete_skill, name)
    return skills.dumps(result)


_HANDLERS = {
    "evaluate": _tool_evaluate,
    "get_chat_info": _tool_get_chat_info,
    "get_user_info": _tool_get_user_info,
    "get_profile": _tool_get_profile,
    "run_shell": _tool_run_shell,
    "web_search": _tool_web_search,
    "fetch_url": _tool_fetch_url,
    "get_time": _tool_get_time,
    "text_stats": _tool_text_stats,
    "get_bot_stats": _tool_get_bot_stats,
    "memory_remember": _tool_memory_remember,
    "memory_recall": _tool_memory_recall,
    "memory_forget": _tool_memory_forget,
    "memory_list": _tool_memory_list,
    "save_skill": _tool_save_skill,
    "load_skill": _tool_load_skill,
    "list_skills": _tool_list_skills,
    "delete_skill": _tool_delete_skill,
    "run_subagent": _tool_run_subagent,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "list_dir": _tool_list_dir,
    "search_files": _tool_search_files,
    "execute_script": _tool_execute_script,
}


async def execute_tool(
    name,
    arguments,
    chat_id,
    client=None,
    stats=None,
    unrestricted=False,
    allowed=None,
):
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"Неизвестная функция: {name}"
    if allowed is not None and name not in allowed:
        logger.warning("Инструмент %s не разрешён в этой сессии", name)
        return f"Инструмент недоступен в этой сессии: {name}"
    if unrestricted is not True and name in OWNER_ONLY_TOOLS:
        logger.warning("Инструмент %s доступен только владельцу", name)
        return f"Инструмент {name} доступен только владельцу."
    try:
        return await handler(arguments, chat_id, client, stats, unrestricted)
    except TOOL_ERRORS as exc:
        logger.exception("Инструмент %s упал", name)
        return f"Ошибка инструмента {name}: {type(exc).__name__}: {exc}"


def tool_names_of(schema) -> set:
    names = set()
    for item in schema:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names
