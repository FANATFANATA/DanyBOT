import asyncio
import json
import logging
from typing import Any, cast

import core

logger = logging.getLogger("danybot.subagents")

SUBAGENT_SYSTEM = (
    "Ты - универсальный субагент DanyBOT. Ты автономно решаешь одну задачу и "
    "можешь вызывать инструменты. Действуй по шагам, проверяй факты "
    "инструментами, не выдумывай данные. Когда задача решена, верни краткий "
    "итоговый ответ на языке задачи, без описания процесса."
)

MAX_TASKS = 16
MAX_TOOL_RESULT = 6000
MAX_CONTEXT_MESSAGES = 40
REQUEST_TIMEOUT = 120.0

TRUNCATED_MARK = "\n… (вывод обрезан)"

_RUNTIME: dict[str, Any] = {
    "ai": None,
    "model": "",
    "verifier": None,
    "stats": None,
    "max_rounds": None,
    "max_tokens": 4096,
    "concurrency": None,
    "enabled": True,
    "timeout": REQUEST_TIMEOUT,
}


def configure(
    ai=None,
    model=None,
    verifier=None,
    stats=None,
    max_rounds=None,
    max_tokens=None,
    concurrency=None,
    enabled=None,
    timeout=None,
):
    if ai is not None:
        _RUNTIME["ai"] = ai
    if model is not None:
        _RUNTIME["model"] = model
    if verifier is not None:
        _RUNTIME["verifier"] = verifier
    if stats is not None:
        _RUNTIME["stats"] = stats
    if max_rounds is not None:
        _RUNTIME["max_rounds"] = max(1, int(max_rounds))
    if max_tokens is not None:
        _RUNTIME["max_tokens"] = max(64, int(max_tokens))
    if concurrency is not None:
        _RUNTIME["concurrency"] = max(1, int(concurrency))
    if enabled is not None:
        _RUNTIME["enabled"] = bool(enabled)
    if timeout is not None:
        _RUNTIME["timeout"] = max(1.0, float(timeout))


def is_configured() -> bool:
    return bool(_RUNTIME["enabled"] and _RUNTIME["ai"])


_TOOLS_CACHE: dict[str, Any] = {"key": None, "items": []}


def _available_tools():
    import tools as tools_module

    schema = tools_module.TOOLS
    key = tuple(item["function"]["name"] for item in schema)
    if _TOOLS_CACHE["key"] != key:
        _TOOLS_CACHE["key"] = key
        _TOOLS_CACHE["items"] = [
            item
            for item in schema
            if item["function"]["name"] not in tools_module.SUBAGENT_EXCLUDED_TOOLS
        ]
    return cast(Any, _TOOLS_CACHE["items"])


def _full_tools():
    import tools as tools_module

    return [
        item
        for item in [*tools_module.FILE_TOOLS, *tools_module.TOOLS]
        if item["function"]["name"] not in tools_module.SUBAGENT_EXCLUDED_UNRESTRICTED
    ]


def _select_tools(tool_names, unrestricted=False):
    available = _full_tools() if unrestricted else list(_available_tools())
    if not tool_names:
        return available
    wanted = {str(name).strip() for name in tool_names if str(name).strip()}
    return [item for item in available if item["function"]["name"] in wanted]


def _loads(raw):
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


async def _call_tool(
    tools_module,
    verifier,
    name,
    arguments,
    use_model,
    chat_id,
    client,
    stats,
    allowed,
    verify,
    unrestricted=False,
):
    if verifier is not None and verify and not unrestricted:
        try:
            approved = await verifier(name, arguments, use_model)
        except (OSError, ValueError, TypeError):
            approved = False
        if not approved:
            return name, "Вызов отклонён проверкой безопасности.", False
    try:
        output = await tools_module.execute_tool(
            name, arguments, chat_id, client, stats, unrestricted, allowed
        )
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        output = f"Ошибка инструмента {name}: {exc}"
    text = str(output)
    if len(text) > MAX_TOOL_RESULT:
        text = text[:MAX_TOOL_RESULT] + TRUNCATED_MARK
    return name, text, True


def _assistant_message(content, tool_calls):
    calls = []
    for tc in tool_calls:
        function = getattr(tc, "function", None)
        calls.append(
            {
                "id": getattr(tc, "id", "") or "",
                "type": "function",
                "function": {
                    "name": getattr(function, "name", "") or "",
                    "arguments": getattr(function, "arguments", "") or "{}",
                },
            }
        )
    return {"role": "assistant", "content": content or "", "tool_calls": calls}


async def run_subagent(
    task,
    system=None,
    model=None,
    tool_names=None,
    chat_id=None,
    client=None,
    max_rounds=None,
    subagent_name="universal",
    verify=True,
    stats=None,
    unrestricted=False,
):
    result = {
        "name": subagent_name,
        "task": task,
        "ok": False,
        "result": "",
        "rounds": 0,
        "tools_used": [],
    }
    if not task or not str(task).strip():
        result["result"] = "Пустая задача."
        return result
    if not is_configured():
        result["result"] = "Субагенты недоступны."
        return result

    import tools as tools_module

    ai = _RUNTIME["ai"]
    selected_tools = _select_tools(tool_names, unrestricted=unrestricted)
    use_model = model or _RUNTIME["model"]
    rounds_limit = max_rounds if max_rounds is not None else _RUNTIME["max_rounds"]
    verifier = cast(Any, _RUNTIME["verifier"])
    tool_stats = stats if stats is not None else _RUNTIME["stats"]
    allowed = tools_module.tool_names_of(selected_tools)
    logger.debug("Субагент %s: %s", subagent_name, str(task)[:120])

    messages = [
        {"role": "system", "content": system or SUBAGENT_SYSTEM},
        {"role": "user", "content": str(task)},
    ]
    content = ""
    round_index = 0
    while rounds_limit is None or round_index < rounds_limit:
        round_index += 1
        result["rounds"] = round_index
        if round_index > 1:
            messages = core.trim_tool_history(
                messages, MAX_CONTEXT_MESSAGES, head_size=2
            )
        try:
            raw = await asyncio.wait_for(
                ai.chat.completions.create(
                    model=use_model,
                    messages=cast(Any, messages),
                    temperature=0.7,
                    max_tokens=_RUNTIME["max_tokens"],
                    stream=False,
                    tools=cast(Any, selected_tools),
                ),
                timeout=_RUNTIME["timeout"],
            )
            message = raw.choices[0].message
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            IndexError,
            asyncio.TimeoutError,
        ) as exc:
            logger.warning("Субагент %s: ошибка: %s", subagent_name, exc)
            result["result"] = f"Ошибка субагента: {exc}"
            return result

        content = getattr(message, "content", "") or ""
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            result["ok"] = True
            result["result"] = content.strip()
            return result

        messages.append(_assistant_message(content, tool_calls))
        calls = []
        for tc in tool_calls:
            function = getattr(tc, "function", None)
            calls.append(
                (
                    getattr(tc, "id", "") or "",
                    getattr(function, "name", "") or "",
                    _loads(getattr(function, "arguments", "")),
                )
            )
        outcomes = await asyncio.gather(
            *(
                _call_tool(
                    tools_module,
                    verifier,
                    name,
                    arguments,
                    use_model,
                    chat_id,
                    client,
                    tool_stats,
                    allowed,
                    verify,
                    unrestricted,
                )
                for _call_id, name, arguments in calls
            ),
            return_exceptions=True,
        )
        for (call_id, name, _arguments), outcome in zip(calls, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                logger.warning(
                    "Субагент %s: сбой инструмента %s: %r", subagent_name, name, outcome
                )
                content_text = f"Ошибка инструмента {name}: {outcome}"
            else:
                used_name, content_text, ran = outcome
                if ran:
                    result["tools_used"].append(used_name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content_text,
                }
            )

    result["result"] = content.strip() or "Достигнут лимит шагов субагента."
    return result


async def _gather_workers(fn, specs):
    results = await asyncio.gather(
        *(fn(spec) for spec in specs), return_exceptions=True
    )
    out = []
    for spec, outcome in zip(specs, results, strict=True):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            logger.warning(
                "Субагент %s провалился: %r", spec.get("name", "universal"), outcome
            )
            out.append(
                {
                    "name": spec.get("name", "universal"),
                    "task": spec.get("task", ""),
                    "ok": False,
                    "result": f"Ошибка субагента: {outcome}",
                    "rounds": 0,
                    "tools_used": [],
                }
            )
            continue
        out.append(outcome)
    return out


async def run_subagents(
    tasks,
    concurrency=None,
    system=None,
    model=None,
    tool_names=None,
    chat_id=None,
    client=None,
    max_rounds=None,
    verify=True,
    stats=None,
    unrestricted=False,
):
    if isinstance(tasks, str):
        tasks = [tasks]
    if not isinstance(tasks, list):
        return []
    specs = []
    for item in tasks[:MAX_TASKS]:
        if isinstance(item, dict):
            specs.append(item)
        elif item:
            specs.append({"task": str(item)})
    if not specs:
        return []

    limit = concurrency if concurrency is not None else _RUNTIME["concurrency"]
    logger.info(
        "Запуск %d субагентов (параллельно до %s)",
        len(specs),
        "без лимита" if limit is None else limit,
    )

    async def worker(spec):
        return await run_subagent(
            spec.get("task", ""),
            system=spec.get("system", system),
            model=spec.get("model", model),
            tool_names=spec.get("tools", tool_names),
            chat_id=spec.get("chat_id", chat_id),
            client=client,
            max_rounds=spec.get("max_rounds", max_rounds),
            subagent_name=spec.get("name", "universal"),
            verify=verify,
            stats=stats,
            unrestricted=spec.get("unrestricted", unrestricted),
        )

    if limit is None:
        return await _gather_workers(worker, specs)

    semaphore = asyncio.Semaphore(max(1, int(limit)))

    async def gated(spec):
        async with semaphore:
            return await worker(spec)

    return await _gather_workers(gated, specs)
