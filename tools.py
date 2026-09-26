import ast
import asyncio
import contextlib
import datetime
import html
import json
import logging
import math
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from telethon.errors import RPCError

logger = logging.getLogger("danybot.tools")

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
}

SAFE_CONSTS = {
    "pi": math.pi,
    "e": math.e,
}

CODER_ROOT = Path(os.getenv("CODER_ROOT", "/root")).expanduser().resolve()
MAX_WRITE_BYTES = 500_000
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_FILES = 4000
MAX_SCRIPT_BYTES = 200_000
MAX_SCRIPT_OUTPUT = 4000


def _resolve_path(raw, root=None):
    base = (root or CODER_ROOT).resolve()
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
        if node.id in SAFE_CONSTS:
            return SAFE_CONSTS[node.id]
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


def _int_arg(arguments, key, default, lo, hi):
    try:
        value = int(arguments.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _str_arg(arguments, key, default=""):
    return str(arguments.get(key, default)).strip()


def render_response(
    prefix,
    reasoning_parts,
    tool_parts,
    full_answer,
    show_reasoning=True,
    show_tools=True,
):
    reasoning = _clip("".join(reasoning_parts), 3000) if show_reasoning else ""
    answer = _clip(full_answer, 3000)
    parts = []
    if reasoning:
        parts.append(f"reasoning:\n{reasoning}")
    if show_tools:
        seen = []
        for tool in tool_parts:
            if tool not in seen:
                seen.append(tool)
        if seen:
            parts.append("tools: " + ", ".join(seen))
    if answer:
        parts.append(answer)
    text = prefix + "\n\n".join(parts)
    if len(text) > 4000:
        for drop in (0, 1):
            if drop < len(parts):
                kept = [p for i, p in enumerate(parts) if i != drop]
                candidate = prefix + "\n\n".join(kept)
                if len(candidate) <= 4000:
                    text = candidate
                    break
    if len(text) > 4000:
        text = text[:3997] + "…"
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
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
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
                "набором инструментов. Вернуть результаты всех задач."
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
                        "maximum": 20,
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
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
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

SUBAGENT_EXCLUDED_TOOLS = frozenset(
    {"run_subagent", *FILE_TOOL_NAMES, *MEMORY_TOOL_NAMES}
)


async def _tool_evaluate(arguments, chat_id, client, stats):
    return safe_eval(_str_arg(arguments, "expression"))


async def _tool_get_chat_info(arguments, chat_id, client, stats):
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


async def _tool_get_user_info(arguments, chat_id, client, stats):
    handle = _str_arg(arguments, "handle")
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


async def _tool_get_profile(arguments, chat_id, client, stats):
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
        },
        ensure_ascii=False,
    )


async def _tool_run_shell(arguments, chat_id, client, stats):
    command = _str_arg(arguments, "command")
    if not command:
        return "Пустая команда."
    timeout = _int_arg(arguments, "timeout", 30, 1, 120)
    workdir = None
    raw_cwd = _str_arg(arguments, "cwd")
    if raw_cwd:
        workdir, err = _resolve_path(raw_cwd)
        if err or workdir is None:
            return err
        if not workdir.is_dir():
            return f"Каталог не найден: {workdir}"
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir) if workdir else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return f"Ошибка запуска: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return f"Таймаут {timeout}s: команда прервана."
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    result = f"rc={proc.returncode}\nstdout:\n{out}"
    if err:
        result += f"\nstderr:\n{err}"
    return result[:4000]


_httpx_singleton = None


def _get_httpx_client():
    global _httpx_singleton
    if _httpx_singleton is None or _httpx_singleton.is_closed:
        _httpx_singleton = httpx.AsyncClient(
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
    return _httpx_singleton


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


async def _tool_web_search(arguments, chat_id, client, stats):
    query = _str_arg(arguments, "query")
    if not query:
        return "Пустой запрос."
    limit = _int_arg(arguments, "limit", 5, 1, 10)
    hc = _get_httpx_client()
    errors = []
    for url, parser in ((BRAVE_SEARCH_URL, _parse_brave), (DDG_SEARCH_URL, _parse_ddg)):
        try:
            resp = await hc.get(url, params={"q": query}, timeout=20)
            resp.raise_for_status()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        results = parser(resp.text, limit)
        if results:
            return "\n\n".join(results)
        errors.append(f"{url}: пустая выдача")
    logger.warning("web_search без результатов: %s", "; ".join(errors))
    if len(errors) == 2 and all("пустая выдача" not in e for e in errors):
        return f"Ошибка поиска: {errors[0]}"
    return "Ничего не найдено."


async def _tool_fetch_url(arguments, chat_id, client, stats):
    url = _str_arg(arguments, "url")
    if not url:
        return "Пустой URL."
    if not url.startswith(("http://", "https://")):
        return "URL должен начинаться с http:// или https://"
    max_chars = _int_arg(arguments, "max_chars", 5000, 100, 20000)
    try:
        hc = _get_httpx_client()
        resp = await hc.get(url, timeout=25)
        resp.raise_for_status()
        raw = resp.text
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return f"Ошибка загрузки: {exc}"
    cleaned = re.sub(
        r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE
    )
    cleaned = re.sub(
        r"<style[^>]*>.*?</style>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE
    )
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


async def _tool_get_time(arguments, chat_id, client, stats):
    utc = datetime.datetime.now(datetime.timezone.utc)
    return json.dumps(
        {
            "utc": utc.isoformat(),
            "local": utc.astimezone().isoformat(),
            "weekday": utc.strftime("%A"),
        },
        ensure_ascii=False,
    )


async def _tool_text_stats(arguments, chat_id, client, stats):
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


async def _tool_get_bot_stats(arguments, chat_id, client, stats):
    data = stats(chat_id) if stats is not None else {}
    return json.dumps(data, ensure_ascii=False)


async def _tool_run_subagent(arguments, chat_id, client, stats):
    import subagents
    import userbot as userbot_module

    if not subagents.is_configured():
        return "Субагенты недоступны."
    tasks = arguments.get("tasks")
    if not tasks:
        single = _str_arg(arguments, "task")
        tasks = [single] if single else []
    if not tasks:
        return "Нужна задача: task или tasks."
    results = await subagents.run_subagents(
        tasks,
        concurrency=arguments.get("concurrency"),
        system=_str_arg(arguments, "system") or None,
        model=_str_arg(arguments, "model") or None,
        tool_names=arguments.get("tools"),
        chat_id=chat_id,
        client=client,
        max_rounds=arguments.get("max_rounds"),
        verify=not userbot_module.is_unrestricted(chat_id),
    )
    return json.dumps(results, ensure_ascii=False)[:8000]


async def _tool_read_file(arguments, chat_id, client, stats):
    path, err = _resolve_path(arguments.get("path"))
    if err or path is None:
        return err
    if not path.exists():
        return f"Файл не найден: {path}"
    if path.is_dir():
        return f"Это каталог: {path}. Используй list_dir."
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
    return f"{head}\n{body}"


async def _tool_write_file(arguments, chat_id, client, stats):
    path, err = _resolve_path(arguments.get("path"))
    if err or path is None:
        return err
    content = str(arguments.get("content", ""))
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return f"Слишком большой объём: {len(content.encode('utf-8'))} байт"
    if path.is_dir():
        return f"Это каталог: {path}"
    existed = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except (OSError, ValueError) as exc:
        return f"Ошибка записи: {exc}"
    action = "Перезаписан" if existed else "Создан"
    return f"{action}: {path} ({len(content)} символов)"


async def _tool_edit_file(arguments, chat_id, client, stats):
    path, err = _resolve_path(arguments.get("path"))
    if err or path is None:
        return err
    if not path.is_file():
        return f"Файл не найден: {path}"
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
    try:
        path.write_text(updated, encoding="utf-8")
    except (OSError, ValueError) as exc:
        return f"Ошибка записи: {exc}"
    return f"Изменён: {path} (замен {count if replace_all else 1})"


async def _tool_list_dir(arguments, chat_id, client, stats):
    path, err = _resolve_path(arguments.get("path", "."))
    if err or path is None:
        return err
    if not path.exists():
        return f"Каталог не найден: {path}"
    if path.is_file():
        return f"Это файл: {path}. Используй read_file."
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as exc:
        return f"Ошибка чтения каталога: {exc}"
    rows = []
    for entry in entries[:MAX_LIST_ENTRIES]:
        try:
            rows.append(
                f"{entry.name}/"
                if entry.is_dir()
                else f"{entry.name} ({entry.stat().st_size})"
            )
        except OSError:
            rows.append(entry.name)
    head = f"{path} | элементов {len(entries)}"
    if len(entries) > MAX_LIST_ENTRIES:
        head += f", показано {MAX_LIST_ENTRIES}"
    if not rows:
        return f"{head}\n(пусто)"
    return head + "\n" + "\n".join(rows)


async def _tool_search_files(arguments, chat_id, client, stats):
    pattern = _str_arg(arguments, "pattern")
    if not pattern:
        return "Пустой pattern."
    path, err = _resolve_path(arguments.get("path", "."))
    if err or path is None:
        return err
    if not path.exists():
        return f"Путь не найден: {path}"
    glob_pat = _str_arg(arguments, "glob") or "*"
    limit = _int_arg(arguments, "limit", 60, 1, MAX_SEARCH_RESULTS)
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"Некорректное выражение: {exc}"
    candidates = [path] if path.is_file() else sorted(path.rglob(glob_pat))
    matches = []
    scanned = 0
    for item in candidates:
        if len(matches) >= limit or scanned >= MAX_SEARCH_FILES:
            break
        if not item.is_file():
            continue
        scanned += 1
        try:
            text = item.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                matches.append(f"{item}:{idx}: {line.strip()[:200]}")
                if len(matches) >= limit:
                    break
    if not matches:
        return "Совпадений не найдено."
    return "\n".join(matches)


async def _tool_execute_script(arguments, chat_id, client, stats):
    code = str(arguments.get("code", ""))
    if not code.strip():
        return "Пустой код."
    encoded = code.encode("utf-8")
    if len(encoded) > MAX_SCRIPT_BYTES:
        return f"Слишком большой объём: {len(encoded)} байт"
    timeout = _int_arg(arguments, "timeout", 30, 1, 120)
    workdir = CODER_ROOT
    raw_cwd = _str_arg(arguments, "cwd")
    if raw_cwd:
        workdir, err = _resolve_path(raw_cwd)
        if err or workdir is None:
            return err
        if not workdir.is_dir():
            return f"Каталог не найден: {workdir}"
    script_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(code)
            script_path = Path(handle.name)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script_path),
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        if script_path is not None:
            with contextlib.suppress(OSError):
                script_path.unlink()
        return f"Ошибка запуска: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return f"Таймаут {timeout}s: скрипт прерван."
    finally:
        if script_path is not None:
            with contextlib.suppress(OSError):
                script_path.unlink()
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    result = f"rc={proc.returncode}\nstdout:\n{out}"
    if err:
        result += f"\nstderr:\n{err}"
    return _clip(result, MAX_SCRIPT_OUTPUT)


async def _tool_memory_remember(arguments, chat_id, client, stats):
    import memory

    key = _str_arg(arguments, "key")
    value = str(arguments.get("value", ""))
    tags = arguments.get("tags")
    result = memory.remember(chat_id, key, value, tags)
    return memory.dumps(result)


async def _tool_memory_recall(arguments, chat_id, client, stats):
    import memory

    key = _str_arg(arguments, "key")
    query = _str_arg(arguments, "query")
    limit = _int_arg(arguments, "limit", 10, 1, 200)
    result = memory.recall(chat_id, key=key, query=query, limit=limit)
    return memory.dumps(result)


async def _tool_memory_forget(arguments, chat_id, client, stats):
    import memory

    key = _str_arg(arguments, "key")
    mem_id = _int_arg(arguments, "id", 0, 0, 10**9)
    result = memory.forget(chat_id, key=key, mem_id=mem_id)
    return memory.dumps(result)


async def _tool_memory_list(arguments, chat_id, client, stats):
    import memory

    limit = _int_arg(arguments, "limit", 50, 1, 200)
    result = memory.list_memories(chat_id, limit=limit)
    result["stats"] = memory.stats(chat_id)
    return memory.dumps(result)


async def _tool_save_skill(arguments, chat_id, client, stats):
    import skills

    name = _str_arg(arguments, "name")
    description = _str_arg(arguments, "description")
    body = str(arguments.get("body", ""))
    tags = arguments.get("tags")
    result = skills.save_skill(name, description, body, tags)
    return skills.dumps(result)


async def _tool_load_skill(arguments, chat_id, client, stats):
    import skills

    name = _str_arg(arguments, "name")
    result = skills.load_skill(name)
    return skills.dumps(result)


async def _tool_list_skills(arguments, chat_id, client, stats):
    import skills

    tag = _str_arg(arguments, "tag")
    query = _str_arg(arguments, "query")
    limit = _int_arg(arguments, "limit", 50, 1, 200)
    result = skills.list_skills(tag=tag, query=query, limit=limit)
    result["stats"] = skills.stats()
    return skills.dumps(result)


async def _tool_delete_skill(arguments, chat_id, client, stats):
    import skills

    name = _str_arg(arguments, "name")
    result = skills.delete_skill(name)
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


async def execute_tool(name, arguments, chat_id, client=None, stats=None):
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"Неизвестная функция: {name}"
    return await handler(arguments, chat_id, client, stats)
