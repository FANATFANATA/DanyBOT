import asyncio
import json
import logging
from typing import Any, cast

logger = logging.getLogger("danybot.subagents")

SUBAGENT_SYSTEM = (
    "Ты - универсальный субагент DanyBOT. Ты автономно решаешь одну задачу и "
    "можешь вызывать инструменты. Действуй по шагам, проверяй факты "
    "инструментами, не выдумывай данные. Когда задача решена, верни краткий "
    "итоговый ответ на языке задачи, без описания процесса."
)

MAX_TASKS = 16
MAX_TOOL_RESULT = 6000
REQUEST_TIMEOUT = 120.0

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


_TOOLS_AVAILABLE = None


def _select_tools(tool_names):
    global _TOOLS_AVAILABLE
    if _TOOLS_AVAILABLE is None:
        import tools as tools_module

        _TOOLS_AVAILABLE = [
            item
            for item in tools_module.TOOLS
            if item["function"]["name"] not in tools_module.SUBAGENT_EXCLUDED_TOOLS
        ]
    available = list(_TOOLS_AVAILABLE)
    if not tool_names:
        return available
    wanted = {str(name).strip() for name in tool_names if str(name).strip()}
    selected = [item for item in available if item["function"]["name"] in wanted]
    return selected or available


def _loads(raw):
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


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
    selected_tools = _select_tools(tool_names)
    use_model = model or _RUNTIME["model"]
    rounds_limit = max_rounds if max_rounds is not None else _RUNTIME["max_rounds"]
    verifier = cast(Any, _RUNTIME["verifier"])
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
        for tc in tool_calls:
            function = getattr(tc, "function", None)
            name = getattr(function, "name", "") or ""
            arguments = _loads(getattr(function, "arguments", ""))
            call_id = getattr(tc, "id", "") or ""
            if verifier is not None:
                allowed = True
                if verify:
                    try:
                        allowed = await verifier(name, arguments, use_model)
                    except (OSError, ValueError, TypeError):
                        allowed = False
                if not allowed:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": "Вызов отклонён проверкой безопасности.",
                        }
                    )
                    continue
            try:
                output = await tools_module.execute_tool(
                    name, arguments, chat_id, client, _RUNTIME["stats"]
                )
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                output = f"Ошибка инструмента {name}: {exc}"
            result["tools_used"].append(name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(output)[:MAX_TOOL_RESULT],
                }
            )

    result["result"] = content.strip() or "Достигнут лимит шагов субагента."
    return result


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
    logger.info("Запуск %d субагентов (параллельно до %s)", len(specs), limit)

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
        )

    if limit is None:
        return await asyncio.gather(*(worker(spec) for spec in specs))

    semaphore = asyncio.Semaphore(max(1, int(limit)))

    async def gated(spec):
        async with semaphore:
            return await worker(spec)

    return await asyncio.gather(*(gated(spec) for spec in specs))
