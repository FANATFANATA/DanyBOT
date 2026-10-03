import argparse
import asyncio
import json
import logging
import math
import os
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from unittest import mock

import httpx
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    InlineQuery,
    Message,
    Update,
    User,
)
from aiogram.utils.token import TokenValidationError
from telethon.crypto import AuthKey
from telethon.errors import AuthKeyError, FloodWaitError, RPCError
from telethon.sessions import MemorySession, StringSession

import bot
import core
import memory
import proxies
import skills
import subagents
import tools as tools_module
import userbot

PROJECT_DIR = Path(__file__).resolve().parent
PY_FILES = (
    "main.py",
    "bot.py",
    "userbot.py",
    "proxies.py",
    "tools.py",
    "core.py",
    "subagents.py",
    "memory.py",
    "skills.py",
    "tests.py",
)
WHITELIST_FILE = "vulture_whitelist.py"
REQUIREMENTS_FILE = "requirements.txt"
COVERAGE_SOURCE = ",".join(
    path.removesuffix(".py") for path in PY_FILES if path not in ("tests.py",)
)
BANDIT_SKIP = "B104,B404,B603,B607,B608"
VULTURE_IGNORE_NAMES = "test_*,setUp"
LOG_FILE = PROJECT_DIR / "toolrun.log"
AUTH_PART = "123456"
BOT_AUTH_VALUE = "bot-token"
BOT_TOKEN_VALUE = f"{AUTH_PART}:{BOT_AUTH_VALUE}"
NO_AUTH = ""
_log_lines: list[str] = []


def _raise_oserror(*_args, **_kwargs):
    raise OSError(28, "No space left on device")


def log_open():
    global _log_lines
    _log_lines = [
        (
            f"DanyBOT self-check :: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"python {sys.version.split()[0]} :: {sys.platform}\n"
            f"cwd: {PROJECT_DIR}\n"
        ),
    ]


def log_close():
    global _log_lines
    if _log_lines:
        LOG_FILE.write_text("".join(_log_lines), encoding="utf-8")
        _log_lines = []


def _log_write(text):
    _log_lines.append(text)


def _cmd_str(cmd):
    return " ".join(str(part) for part in cmd)


def run_logged(cmd):
    started = time.perf_counter()
    _log_write(f"\n$ {_cmd_str(cmd)}\n")
    proc = subprocess.run(
        cmd,
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    elapsed = time.perf_counter() - started
    _log_write(proc.stdout or "")
    _log_write(proc.stderr or "")
    _log_write(f"[rc={proc.returncode}] {elapsed:.2f}s\n")
    return proc, elapsed


def _reconfigure_stdio():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def patch_paths(testcase):
    tmp = Path(tempfile.mkdtemp(prefix="danybot_tests_"))
    testcase.addCleanup(shutil.rmtree, tmp, True)
    saved = []
    for mod, attr, fname in (
        (proxies, "CACHE_FILE", "working_proxies.json"),
        (proxies, "RAW_CACHE_FILE", "proxy_cache.txt"),
        (userbot, "STATE_FILE", "state_userbot.json"),
        (userbot, "HISTORY_FILE", "history_userbot.json"),
        (bot, "STATE_FILE", "state_bot.json"),
        (bot, "HISTORY_FILE", "history_bot.json"),
    ):
        saved.append((mod, attr, getattr(mod, attr)))
        setattr(mod, attr, tmp / fname)

    def restore():
        for mod2, attr2, old in saved:
            setattr(mod2, attr2, old)

    testcase.addCleanup(restore)
    return tmp


_MODE_DICT_ATTRS = (
    "model_overrides",
    "chat_history",
    "last_chat_activity",
    "inline_seen",
)
_MODE_SET_ATTRS = (
    "coder_chats",
    "reasoning_hidden",
    "tools_hidden",
    "recent_reply_ids",
    "seen_msg_keys",
)
_MODE_BOOL_ATTRS = ("inline_mode",)
_ABSENT_KEY = "__absent_attrs__"


def _snapshot_module(mod):
    snap = {}
    absent = set()
    for attr in _MODE_DICT_ATTRS:
        if not hasattr(mod, attr):
            absent.add(attr)
            continue
        value = getattr(mod, attr)
        if attr == "chat_history":
            snap[attr] = {k: deque(v, maxlen=v.maxlen) for k, v in value.items()}
        else:
            snap[attr] = dict(value)
    for attr in _MODE_SET_ATTRS:
        if not hasattr(mod, attr):
            absent.add(attr)
            continue
        snap[attr] = set(getattr(mod, attr))
    for attr in _MODE_BOOL_ATTRS:
        if hasattr(mod, attr):
            snap[attr] = getattr(mod, attr)
        else:
            absent.add(attr)
    snap[_ABSENT_KEY] = absent
    return snap


def _restore_module(mod, snap):
    absent = snap.get(_ABSENT_KEY) or set()
    for attr, value in snap.items():
        if attr == _ABSENT_KEY:
            continue
        setattr(mod, attr, value)
    for attr in absent:
        try:
            delattr(mod, attr)
        except AttributeError:
            pass


def snapshot_mode_state():
    return {
        "userbot": _snapshot_module(userbot),
        "bot": _snapshot_module(bot),
        "MODELS": list(userbot.MODELS),
    }


def restore_mode_state(snap):
    _restore_module(userbot, snap["userbot"])
    _restore_module(bot, snap["bot"])
    userbot.MODELS = snap["MODELS"]


def btn_data(btn):
    data = getattr(btn, "callback_data", None)
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)


def rows_data(markup):
    return {btn_data(btn) for btn in rows_data_buttons(markup)}


def rows_data_buttons(markup):
    return [btn for row in markup.inline_keyboard for btn in row]


def menu_commands():
    return [item.command for item in bot._menu_commands()]


def menu_block():
    source = (PROJECT_DIR / "bot.py").read_text(encoding="utf-8")
    start = source.index("return [", source.index("def _menu_commands()"))
    return source[start : source.index("]", start)]


class BotTestCase(unittest.TestCase):
    def setUp(self):
        patch_paths(self)
        self._snap = snapshot_mode_state()
        saved_journals = {mod: mod.TASKS.snapshot() for mod in (userbot, bot)}

        def restore_snap():
            restore_mode_state(self._snap)
            for mod, saved in saved_journals.items():
                mod.TASKS.restore(saved)

        self.addCleanup(restore_snap)
        for mod in (userbot, bot):
            mod.TASKS.restore({})


class _NullAsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeClient:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.history_limits = []

    async def edit_message(self, chat, msg_id, text, **kwargs):
        self.edited.append((chat, msg_id, text))
        return True

    async def get_messages(self, chat, limit=20, ids=None):
        self.history_limits.append(limit)
        if ids is not None:
            if ids[0] == 42:
                return [SimpleNamespace(id=42, sender_id=5, message="found")]
            return []
        return [
            SimpleNamespace(id=i, sender_id=i % 3, message=f"t{i}")
            for i in range(limit, 0, -1)
        ]

    async def get_entity(self, key):
        return SimpleNamespace(
            id=7,
            title="Chat T",
            username="uchat",
            participants_count=11,
            first_name="A",
            last_name="B",
        )

    async def get_me(self):
        return SimpleNamespace(
            id=1,
            first_name="Me",
            last_name="My",
            username="meuser",
            phone="+79990000000",
        )

    def action(self, chat, _action_name):
        return _NullAsyncContext()


class StreamIter:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = 0

    async def __aiter__(self):
        for c in self._chunks:
            yield c

    async def close(self):
        self.closed += 1


class FakeCompletions:
    def __init__(self, rounds):
        self._rounds = [list(r) for r in rounds]
        self.calls = []
        self.streams = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        stream = StreamIter(self._rounds.pop(0))
        self.streams.append(stream)
        return stream


class FakeAI:
    def __init__(self, rounds):
        self.chat = SimpleNamespace(completions=FakeCompletions(rounds))


def make_delta(content=None, tool_calls=None, reasoning_content=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )


def make_chunk(d):
    return SimpleNamespace(choices=[SimpleNamespace(delta=d)])


def make_tc(index=0, tc_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class EnvHelpersTest(BotTestCase):
    def test_env_int_default_when_unset(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("DANYBOT_TEST_INT", None)
            self.assertEqual(userbot._env_int("DANYBOT_TEST_INT", 7), 7)

    def test_env_int_valid_and_garbage(self):
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_INT": "42"}):
            self.assertEqual(userbot._env_int("DANYBOT_TEST_INT", 7), 42)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_INT": "abc"}):
            self.assertEqual(userbot._env_int("DANYBOT_TEST_INT", 7), 7)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_INT": "3.9"}):
            self.assertEqual(userbot._env_int("DANYBOT_TEST_INT", 7), 7)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_INT": "-5"}):
            self.assertEqual(userbot._env_int("DANYBOT_TEST_INT", 7), -5)

    def test_env_float_valid_garbage_empty(self):
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_FLT": "2.5"}):
            self.assertEqual(userbot._env_float("DANYBOT_TEST_FLT", 1.0), 2.5)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_FLT": "junk"}):
            self.assertEqual(userbot._env_float("DANYBOT_TEST_FLT", 1.0), 1.0)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_FLT": ""}):
            self.assertEqual(userbot._env_float("DANYBOT_TEST_FLT", 1.0), 1.0)

    def test_env_str_strips_and_defaults(self):
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_STR": "  hi  "}):
            self.assertEqual(userbot._env_str("DANYBOT_TEST_STR", "d"), "hi")
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_STR": "   "}):
            self.assertEqual(userbot._env_str("DANYBOT_TEST_STR", "d"), "")
        with mock.patch.dict(os.environ):
            os.environ.pop("DANYBOT_TEST_STR", None)
            self.assertEqual(userbot._env_str("DANYBOT_TEST_STR", "d"), "d")

    def test_env_bool_rules(self):
        for raw, expected in (
            ("on", True),
            ("1", True),
            ("yes", True),
            ("off", False),
            ("0", False),
            ("no", False),
            ("TRUE", True),
            ("", False),
            ("junk", True),
        ):
            with (
                self.subTest(raw=raw),
                mock.patch.dict(os.environ, {"DANYBOT_TEST_BOOL": raw}),
            ):
                self.assertEqual(
                    userbot._env_bool("DANYBOT_TEST_BOOL", False), expected
                )
        with mock.patch.dict(os.environ):
            os.environ.pop("DANYBOT_TEST_BOOL", None)
            self.assertTrue(userbot._env_bool("DANYBOT_TEST_BOOL", True))
            self.assertFalse(userbot._env_bool("DANYBOT_TEST_BOOL", False))

    def test_env_pos_int_ignores_non_positive(self):
        for raw, expected in (
            ("5", 5),
            ("0", None),
            ("-2", None),
            ("", None),
            ("junk", None),
        ):
            with (
                self.subTest(raw=raw),
                mock.patch.dict(os.environ, {"DANYBOT_TEST_POS": raw}),
            ):
                self.assertEqual(userbot._env_pos_int("DANYBOT_TEST_POS"), expected)

    def test_env_id_set_parses_and_skips_junk(self):
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_IDS": " 7 ; 8, 9 ,, bad , 7"}):
            self.assertEqual(userbot._env_id_set("DANYBOT_TEST_IDS"), {7, 8, 9})
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_IDS": ""}):
            self.assertEqual(userbot._env_id_set("DANYBOT_TEST_IDS"), set())

    def test_read_extra_system_reports_missing_file(self):
        self.assertEqual(userbot._read_extra_system(""), "")
        self.assertEqual(
            userbot._read_extra_system(str(Path(tempfile.gettempdir()) / "нет.такого")),
            "",
        )
        tmp = Path(tempfile.mkdtemp(prefix="danybot_prompt_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "extra.md"
        target.write_text("  дополнительный текст  ", encoding="utf-8")
        self.assertEqual(
            userbot._read_extra_system(str(target)), "дополнительный текст"
        )


class MakeSessionTest(unittest.TestCase):
    def test_plain_name_passthrough(self):
        self.assertEqual(userbot.make_session("plainname"), "plainname")

    def test_valid_string_session(self):
        session = StringSession()
        session.set_dc(2, "149.154.167.50", 443)
        session.auth_key = AuthKey(bytes(256))
        raw = session.save()
        result = userbot.make_session(raw)
        self.assertIsInstance(result, StringSession)

    def test_struct_error_fallback(self):
        self.assertEqual(userbot.make_session("1AAAA"), "1AAAA")

    def test_binascii_error_fallback(self):
        self.assertEqual(userbot.make_session("1notvalid@@@"), "1notvalid@@@")


class TriggerRegexTest(unittest.TestCase):
    MATCH_CASES = (
        ".db привет",
        ".ai вопрос",
        ".DB тест",
        "привет\n.ai после строки",
    )
    NO_MATCH_CASES = (
        "",
        ".",
        "x.db привет",
        ".dbx",
        ".dbмодель",
        "abc.ai def",
        ".дб привет",
        ".ДБ привет",
        ".danybot",
        ".gpt?",
        ".bot как дела",
    )

    def test_matches(self):
        for case in self.MATCH_CASES:
            with self.subTest(case=case):
                self.assertIsNotNone(userbot.TRIGGER_RE.search(case))

    def test_no_match(self):
        for case in self.NO_MATCH_CASES:
            with self.subTest(case=case):
                self.assertIsNone(userbot.TRIGGER_RE.search(case))


class HandleCommandsTest(unittest.TestCase):
    CHECKS = (
        (".db clear", ("clear", None)),
        ("  .DB CLEAR ", ("clear", None)),
        (".db clear extra args", ("clear", None)),
        (".db model gpt-x", ("model", "gpt-x")),
        (".db model", ("model", None)),
        (".ai model", ("model", None)),
        (".db coder on", ("coder", True)),
        (".ai coder off", ("coder", False)),
        (".db reasoning on", ("reasoning", True)),
        (".db tools", ("tools_status", None)),
        (".ai prompt", ("prompt", None)),
        (".db models", ("models", None)),
        (".ai help", ("help", None)),
        (".db ?", ("help", None)),
    )
    NONE_CASES = (
        "hello",
        "",
        ".",
        ".db",
        ".ai",
        ".db unknowncmd",
        ".dbmodel",
        ".unknown clear",
    )

    def test_commands_parsed(self):
        for text, expected in self.CHECKS:
            with self.subTest(text=text):
                self.assertEqual(userbot.handle_commands(text), expected)

    def test_commands_none(self):
        for text in self.NONE_CASES:
            with self.subTest(text=text):
                self.assertIsNone(userbot.handle_commands(text))


class StripRoleTagTest(unittest.TestCase):
    def test_cases(self):
        cases = {
            "DanyBOT: ответ": "ответ",
            "danybot: x": "x",
            "[user] q": "q",
            "[SYSTEM] z": "z",
            "обычный текст": "обычный текст",
            "Someone: text": "Someone: text",
        }
        for src, expected in cases.items():
            with self.subTest(src=src):
                self.assertEqual(userbot.strip_role_tag(src), expected)


class SafeEvalTest(unittest.TestCase):
    EXACT_CASES = (
        ("1+2", "3"),
        ("2**10", "1024"),
        ("10//3", "3"),
        ("10%3", "1"),
        ("-(-5)", "5"),
        ("+7", "7"),
        ("sqrt(16)", "4.0"),
        ("abs(-2)", "2"),
        ("min(4,2,9)", "2"),
        ("max(4,2,9)", "9"),
        ("pow(2,10)", "1024"),
        ("round(3.7)", "4"),
        ("pi", str(math.pi)),
        ("e", str(math.e)),
        ("1e400", "inf"),
    )
    ERROR_CASES = (
        "sqrt",
        "__import__('os').system('echo hi')",
        "(lambda: 1)()",
        "'a'",
        "unknown_fn(1)",
        "1/0",
        "2+",
    )

    def test_exact(self):
        for expr, expected in self.EXACT_CASES:
            with self.subTest(expr=expr):
                self.assertEqual(userbot.safe_eval(expr), expected)

    def test_errors(self):
        for expr in self.ERROR_CASES:
            with self.subTest(expr=expr):
                result = userbot.safe_eval(expr)
                self.assertTrue(result.startswith("Ошибка вычисления"), result)

    def test_power_is_bounded(self):
        for expr in ("9**9**9", "pow(2, 999999999)", "10**10**10", "2**(10**9)"):
            with self.subTest(expr=expr):
                result = userbot.safe_eval(expr)
                self.assertTrue(result.startswith("Ошибка вычисления"), result)

    def test_reasonable_power_still_works(self):
        self.assertEqual(userbot.safe_eval("(2**64)**64"), str(2**4096))

    def test_deep_expression_does_not_crash(self):
        result = userbot.safe_eval("1" + "+1" * 20000)
        self.assertTrue(result.startswith("Ошибка вычисления"), result)


class ProxyParsingTest(BotTestCase):
    def test_parse_proxy_lines_filters(self):
        text = (
            "# comment\n"
            "1.2.3.4:8080\n"
            "junk socks://5.6.7.8:1080 tail\n"
            "9.9.9.9:99999\n"
            "\n"
            "not an ip at all\n"
            "8.8.4.4:53\n"
        )
        result = proxies.parse_proxy_lines(text, "socks5")
        self.assertEqual(
            result,
            [
                ("socks5", "1.2.3.4", 8080),
                ("socks5", "5.6.7.8", 1080),
                ("socks5", "8.8.4.4", 53),
            ],
        )

    def test_parse_empty(self):
        self.assertEqual(proxies.parse_proxy_lines("", "http"), [])

    def test_dedupe_keeps_order(self):
        items = [
            ("socks5", "1.1.1.1", 1),
            ("socks5", "1.1.1.1", 1),
            ("http", "2.2.2.2", 2),
            ("socks5", "1.1.1.1", 1),
        ]
        self.assertEqual(proxies.dedupe(items), [items[0], items[2]])

    def test_raw_cache_roundtrip_and_filtering(self):
        proxies.save_raw_cache(
            [
                ("socks5", "1.1.1.1", 1080),
                ("http", "2.2.2.2", 8080),
            ]
        )
        self.assertEqual(
            proxies.load_raw_cache(),
            [
                ("socks5", "1.1.1.1", 1080),
                ("http", "2.2.2.2", 8080),
            ],
        )
        proxies.RAW_CACHE_FILE.write_text(
            "bogus line\nhttp 3.3.3.3 notaport\nsocks4 4.4.4.4 1080\n",
            encoding="utf-8",
        )
        self.assertEqual(proxies.load_raw_cache(), [("socks4", "4.4.4.4", 1080)])

    def test_raw_cache_missing_file(self):
        self.assertEqual(proxies.load_raw_cache(), [])

    def test_proxy_cache_roundtrip(self):
        data = [("socks5", "1.1.1.1", 1080), ("http", "2.2.2.2", 3128)]
        proxies.save_proxy_cache(data)
        self.assertEqual(proxies.load_proxy_cache(), data)

    def test_proxy_cache_corrupt_json(self):
        proxies.CACHE_FILE.write_text("{broken json", encoding="utf-8")
        self.assertEqual(proxies.load_proxy_cache(), [])

    def test_proxy_cache_wrong_structure(self):
        proxies.CACHE_FILE.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(proxies.load_proxy_cache(), [])

    def test_proxy_to_telethon(self):
        self.assertIsNone(proxies.proxy_to_telethon(None))
        self.assertEqual(
            proxies.proxy_to_telethon(("socks5", "h", 1)),
            {"proxy_type": "socks5", "addr": "h", "port": 1},
        )

    def test_telethon_to_item(self):
        self.assertIsNone(proxies.telethon_to_item(None))
        self.assertEqual(
            proxies.telethon_to_item({"proxy_type": "http", "addr": "h", "port": 2}),
            ("http", "h", 2),
        )

    def test_telethon_to_item_survives_broken_data(self):
        self.assertIsNone(proxies.telethon_to_item({}))
        self.assertIsNone(proxies.telethon_to_item({"proxy_type": "http"}))
        self.assertIsNone(
            proxies.telethon_to_item({"proxy_type": "http", "addr": "h", "port": "x"})
        )
        self.assertIsNone(
            proxies.telethon_to_item({"proxy_type": None, "addr": "h", "port": 1})
        )
        self.assertEqual(
            proxies.telethon_to_item(
                {"proxy_type": "http", "addr": "h", "port": "3128"}
            ),
            ("http", "h", 3128),
        )

    def test_mark_bad_proxy_removes_only_target(self):
        proxies.save_proxy_cache(
            [
                ("socks5", "1.1.1.1", 1080),
                ("http", "1.1.1.1", 3128),
                ("socks5", "2.2.2.2", 1080),
            ]
        )
        proxies.mark_bad_proxy(("socks5", "1.1.1.1", 1080))
        self.assertEqual(
            proxies.load_proxy_cache(),
            [
                ("http", "1.1.1.1", 3128),
                ("socks5", "2.2.2.2", 1080),
            ],
        )

    def test_mark_bad_proxy_none_is_noop(self):
        proxies.mark_bad_proxy(None)
        self.assertFalse(proxies.CACHE_FILE.exists())


class ProxyToggleTest(BotTestCase):
    def test_enabled_by_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("PROXY_ENABLED", None)
            self.assertTrue(proxies.proxies_enabled())

    def test_disabled_values(self):
        for value in ("0", "false", "no", "", "FALSE", "No", " ", "off", "OFF"):
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {"PROXY_ENABLED": value}),
            ):
                self.assertFalse(proxies.proxies_enabled())

    def test_enabled_values(self):
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {"PROXY_ENABLED": value}),
            ):
                self.assertTrue(proxies.proxies_enabled())

    def test_disabled_returns_no_candidates(self):
        async def fail_get_working(*_args, **_kwargs):
            raise AssertionError("get_working_proxies не должен вызываться")

        with (
            mock.patch.dict(
                os.environ,
                {
                    "PROXY_ENABLED": "0",
                    "PROXY_HOST": "1.2.3.4",
                    "PROXY_PORT": "1080",
                },
            ),
            mock.patch.object(proxies, "get_working_proxies", fail_get_working),
        ):
            result = asyncio.run(proxies.get_proxy_candidates(limit=5))
        self.assertEqual(result, [])

    def test_enabled_collects_manual_and_auto(self):
        async def fake_get_working(limit=10, prefer_protocol="socks5", deadline=180.0):
            return [("socks5", "9.9.9.9", 1080)]

        with (
            mock.patch.dict(
                os.environ,
                {
                    "PROXY_ENABLED": "1",
                    "PROXY_HOST": "1.2.3.4",
                    "PROXY_PORT": "1080",
                    "PROXY_TYPE": "http",
                    "PROXY_AUTO": "1",
                },
            ),
            mock.patch.object(proxies, "get_working_proxies", fake_get_working),
        ):
            result = asyncio.run(proxies.get_proxy_candidates(limit=5))
        self.assertEqual(
            result,
            [
                {"proxy_type": "http", "addr": "1.2.3.4", "port": 1080},
                {"proxy_type": "socks5", "addr": "9.9.9.9", "port": 1080},
            ],
        )

    def test_static_proxy_settings_are_validated(self):
        async def fail_get_working(*_args, **_kwargs):
            raise AssertionError("get_working_proxies не должен вызываться")

        fallback = {"proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080}
        for env, expected_log, expected in (
            ({"PROXY_TYPE": "ftp", "PROXY_PORT": "1080"}, "PROXY_TYPE=ftp", [fallback]),
            ({"PROXY_TYPE": "socks5", "PROXY_PORT": "0"}, "PROXY_PORT=0", []),
            ({"PROXY_TYPE": "socks5", "PROXY_PORT": "abc"}, "PROXY_PORT=abc", []),
        ):
            with self.subTest(env=env):
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "PROXY_ENABLED": "1",
                            "PROXY_AUTO": "0",
                            "PROXY_HOST": "1.2.3.4",
                            **env,
                        },
                    ),
                    mock.patch.object(proxies, "get_working_proxies", fail_get_working),
                    self.assertLogs("danybot.proxy", level="ERROR") as logs,
                ):
                    result = asyncio.run(proxies.get_proxy_candidates(limit=5))
                self.assertEqual(result, expected)
                self.assertTrue(
                    any(expected_log in line for line in logs.output), logs.output
                )


class ValidateManyTest(BotTestCase):
    @staticmethod
    async def fake_validate_one(proto, host, port, timeout=12):
        return port <= 2

    def test_limit_stops_and_cancels(self):
        items = [
            ("socks5", "h1", 1),
            ("socks5", "h2", 2),
            ("socks5", "h3", 3),
            ("socks5", "h4", 4),
        ]
        with mock.patch.object(proxies, "validate_one", self.fake_validate_one):
            working = asyncio.run(proxies.validate_many(items, limit=2))
        self.assertEqual(len(working), 2)
        self.assertTrue(all(w[2] <= 2 for w in working))

    def test_none_valid_returns_empty(self):
        items = [("socks5", "bad", 99)]
        with mock.patch.object(proxies, "validate_one", self.fake_validate_one):
            working = asyncio.run(proxies.validate_many(items, limit=5))
        self.assertEqual(working, [])

    def test_reaching_limit_cancels_pending(self):
        cancelled = []

        async def validate(proto, host, port, timeout=12):
            if port == 1:
                return True
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(host)
                raise
            return False

        items = [("socks5", "fast", 1)] + [("socks5", f"slow{i}", 9) for i in range(4)]

        async def run():
            return await proxies.validate_many(items, limit=1, concurrency=4)

        with mock.patch.object(proxies, "validate_one", validate):
            working = asyncio.run(run())
        self.assertEqual(working, [("socks5", "fast", 1)])
        self.assertTrue(cancelled)

    @staticmethod
    async def exploding_validate_one(proto, host, port, timeout=12):
        if port == 3:
            raise RuntimeError("unexpected validator crash")
        return port <= 2

    def test_survives_validator_crash(self):
        items = [
            ("socks5", "h1", 1),
            ("socks5", "h2", 2),
            ("socks5", "h3", 3),
        ]
        with mock.patch.object(proxies, "validate_one", self.exploding_validate_one):
            working = asyncio.run(proxies.validate_many(items, limit=5))
        self.assertEqual(sorted(w[2] for w in working), [1, 2])

    def test_validate_one_builds_proxy_dict(self):
        seen = {}

        async def fake_validate(proxy_dict, timeout=12):
            seen["proxy_dict"] = proxy_dict
            seen["timeout"] = timeout
            return True

        with mock.patch.object(proxies, "validate_one_mtproto", fake_validate):
            result = asyncio.run(proxies.validate_one("socks5", "1.2.3.4", 1080, 7))
        self.assertTrue(result)
        self.assertEqual(
            seen["proxy_dict"],
            {"proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080},
        )
        self.assertEqual(seen["timeout"], 7)

    def test_cancel_path_runs_when_pool_exhausted(self):
        started = []

        async def slow_validate(proto, host, port, timeout=12):
            started.append(host)
            await asyncio.sleep(30)
            return False

        async def run():
            items = [("socks5", f"h{i}", i) for i in range(4)]
            task = asyncio.ensure_future(
                proxies.validate_many(items, limit=99, concurrency=2)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with mock.patch.object(proxies, "validate_one", slow_validate):
            asyncio.run(run())
        self.assertEqual(len(started), 2)


class ValidateOneMtprotoTest(BotTestCase):
    PROXY_DICT: ClassVar[dict[str, Any]] = {
        "proxy_type": "socks5",
        "addr": "h",
        "port": 1080,
    }

    def setUp(self):
        super().setUp()
        self._orig_validate_id = proxies.VALIDATE_API_ID
        self._orig_validate_hash = proxies.VALIDATE_API_HASH
        proxies.VALIDATE_API_ID = 1
        proxies.VALIDATE_API_HASH = "hash"
        self.addCleanup(setattr, proxies, "VALIDATE_API_ID", self._orig_validate_id)
        self.addCleanup(setattr, proxies, "VALIDATE_API_HASH", self._orig_validate_hash)

    def test_missing_credentials_skip_validation(self):
        proxies.VALIDATE_API_ID = 0
        proxies.VALIDATE_API_HASH = ""
        with mock.patch.object(proxies, "TelegramClient", self.build_working_client()):
            ok = asyncio.run(proxies.validate_one_mtproto(dict(self.PROXY_DICT)))
        self.assertFalse(ok)

    def test_missing_credentials_warn_once(self):
        saved = proxies._credentials_warned
        proxies._credentials_warned = False
        self.addCleanup(setattr, proxies, "_credentials_warned", saved)
        proxies.VALIDATE_API_ID = 0
        proxies.VALIDATE_API_HASH = ""
        with self.assertLogs("danybot.proxy", level="WARNING") as captured:
            for _ in range(3):
                asyncio.run(proxies.validate_one_mtproto(dict(self.PROXY_DICT)))
        marks = [line for line in captured.output if "VALIDATE_API_ID" in line]
        self.assertEqual(len(marks), 1)

    @staticmethod
    def broken_client_factory(exc):
        class BrokenClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def connect(self):
                raise exc

            async def disconnect(self):
                return None

        return BrokenClient

    @staticmethod
    def build_working_client():
        class WorkingClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def connect(self):
                return True

            async def __call__(self, _request):
                return SimpleNamespace()

            async def disconnect(self):
                return None

        return WorkingClient

    def test_incomplete_read_is_dead_proxy_not_crash(self):
        cls = self.broken_client_factory(asyncio.IncompleteReadError(b"", 8))
        with mock.patch.object(proxies, "TelegramClient", cls):
            ok = asyncio.run(proxies.validate_one_mtproto(dict(self.PROXY_DICT)))
        self.assertFalse(ok)

    def test_eof_error_is_dead_proxy_not_crash(self):
        cls = self.broken_client_factory(EOFError())
        with mock.patch.object(proxies, "TelegramClient", cls):
            ok = asyncio.run(proxies.validate_one_mtproto(dict(self.PROXY_DICT)))
        self.assertFalse(ok)

    def test_unexpected_error_is_dead_proxy_not_crash(self):
        cls = self.broken_client_factory(RuntimeError("boom"))
        with mock.patch.object(proxies, "TelegramClient", cls):
            ok = asyncio.run(proxies.validate_one_mtproto(dict(self.PROXY_DICT)))
        self.assertFalse(ok)

    def test_working_proxy_passes_validation(self):
        cls = self.build_working_client()
        with mock.patch.object(proxies, "TelegramClient", cls):
            ok = asyncio.run(proxies.validate_one_mtproto(dict(self.PROXY_DICT)))
        self.assertTrue(ok)


class ProxyIoTest(BotTestCase):
    def test_cache_write_reports_failure(self):
        target = Path(tempfile.gettempdir()) / "danybot_нет_каталога" / "cache.json"
        self.assertFalse(proxies._cache_write(target, "данные"))

    def test_save_raw_cache_and_load(self):
        target = Path(tempfile.mkdtemp(prefix="danybot_raw_"))
        self.addCleanup(shutil.rmtree, target, True)
        raw = target / "proxy_cache.txt"
        saved = proxies.RAW_CACHE_FILE
        self.addCleanup(setattr, proxies, "RAW_CACHE_FILE", saved)
        proxies.RAW_CACHE_FILE = raw
        rows = [("socks5", "1.2.3.4", 1080), ("http", "5.6.7.8", 8080)]
        proxies.save_raw_cache(rows)
        self.assertEqual(proxies.load_raw_cache(), rows)

    def test_load_raw_cache_skips_bad_lines(self):
        target = Path(tempfile.mkdtemp(prefix="danybot_raw_"))
        self.addCleanup(shutil.rmtree, target, True)
        raw = target / "proxy_cache.txt"
        saved = proxies.RAW_CACHE_FILE
        self.addCleanup(setattr, proxies, "RAW_CACHE_FILE", saved)
        proxies.RAW_CACHE_FILE = raw
        raw.write_text(
            "socks5 1.2.3.4 1080\n"
            "\n"
            "   \n"
            "bad 5.6.7.8 8080\n"
            "http 9.9.9.9 непорт\n"
            "socks4 2.2.2.2 1081\n"
            "http 3.3.3.3 3128 extra\n",
            encoding="utf-8",
        )
        self.assertEqual(
            proxies.load_raw_cache(),
            [
                ("socks5", "1.2.3.4", 1080),
                ("socks4", "2.2.2.2", 1081),
                ("http", "3.3.3.3", 3128),
            ],
        )

    def test_load_raw_cache_handles_missing_file(self):
        saved = proxies.RAW_CACHE_FILE
        self.addCleanup(setattr, proxies, "RAW_CACHE_FILE", saved)
        proxies.RAW_CACHE_FILE = Path(tempfile.gettempdir()) / "нет_такого_кэша.txt"
        self.assertEqual(proxies.load_raw_cache(), [])

    def test_fetch_sources_collects_and_skips_failures(self):
        requested = []

        class _Resp:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, url):
                requested.append(url)
                if "bad" in url:
                    raise httpx.HTTPError("boom")
                return _Resp("1.2.3.4:1080\n")

        saved = proxies.SOURCES
        self.addCleanup(setattr, proxies, "SOURCES", saved)
        proxies.SOURCES = (
            ("socks5", "https://good/list"),
            ("socks4", "https://bad/list"),
        )
        with mock.patch.object(proxies.httpx, "AsyncClient", _Client):
            got = asyncio.run(proxies.fetch_sources())
        self.assertEqual(got, [("socks5", "1.2.3.4", 1080)])
        self.assertEqual(len(requested), 2)

    def test_get_working_proxy_returns_first(self):
        async def fake(limit=10, prefer_protocol="socks5", deadline=180.0):
            return [("socks5", "1.2.3.4", 1080)]

        with mock.patch.object(proxies, "get_working_proxies", fake):
            self.assertEqual(
                asyncio.run(proxies.get_working_proxy()), ("socks5", "1.2.3.4", 1080)
            )

    def test_get_working_proxy_returns_none_when_empty(self):
        async def fake(limit=10, prefer_protocol="socks5", deadline=180.0):
            return []

        with mock.patch.object(proxies, "get_working_proxies", fake):
            self.assertIsNone(asyncio.run(proxies.get_working_proxy()))

    def test_main_reports_missing_proxy(self):
        async def fake(prefer_protocol="socks5"):
            return None

        with mock.patch.object(proxies, "get_working_proxy", fake):
            asyncio.run(proxies.main())

    def test_main_reports_found_proxy(self):
        async def fake(prefer_protocol="socks5"):
            return ("socks5", "1.2.3.4", 1080)

        with mock.patch.object(proxies, "get_working_proxy", fake):
            asyncio.run(proxies.main())

    def test_cli_delegates_to_run_cli(self):
        with mock.patch.object(proxies, "run_cli") as run:
            proxies.cli()
        run.assert_called_once_with()

    def test_run_cli_configures_logging(self):
        with (
            mock.patch.object(proxies.logging, "basicConfig") as basic,
            mock.patch.object(proxies, "main", mock.Mock(return_value=None)),
            mock.patch.object(proxies.asyncio, "run") as run,
        ):
            proxies.run_cli()
        basic.assert_called_once()
        self.assertEqual(basic.call_args.kwargs["level"], logging.INFO)
        run.assert_called_once()


class GetWorkingProxiesTest(BotTestCase):
    def test_cached_hit_skips_fetch(self):
        proxies.save_proxy_cache([("socks5", "1.1.1.1", 1080)])
        marker = [("socks5", "9.9.9.9", 9)]
        seen_args = {}

        async def fake_validate_many(items, limit=10, concurrency=20):
            seen_args["items"] = list(items)
            seen_args["limit"] = limit
            return marker

        def fail_fetch():
            raise AssertionError("fetch_sources не должен вызываться")

        with (
            mock.patch.object(proxies, "validate_many", fake_validate_many),
            mock.patch.object(proxies, "fetch_sources", fail_fetch),
        ):
            result = asyncio.run(proxies.get_working_proxies(limit=3))
        self.assertEqual(result, marker)
        self.assertEqual(seen_args["limit"], 3)
        self.assertEqual(seen_args["items"], [("socks5", "1.1.1.1", 1080)])

    def test_preferred_protocol_sorted_first(self):
        fetched = [
            ("http", "9.9.9.9", 80),
            ("socks5", "8.8.8.8", 1080),
            ("socks4", "7.7.7.7", 1080),
            ("socks5", "6.6.6.6", 1080),
        ]

        async def fake_fetch_sources():
            return fetched

        async def fake_validate_many(items, limit=10, concurrency=20):
            return list(items)[:limit]

        with (
            mock.patch.object(proxies, "fetch_sources", fake_fetch_sources),
            mock.patch.object(proxies, "validate_many", fake_validate_many),
        ):
            result = asyncio.run(
                proxies.get_working_proxies(limit=3, prefer_protocol="socks5")
            )
        self.assertEqual([r[0] for r in result], ["socks5", "socks5", "http"])

    def test_empty_pool_returns_empty(self):
        async def fake_fetch_sources():
            return []

        with mock.patch.object(proxies, "fetch_sources", fake_fetch_sources):
            result = asyncio.run(proxies.get_working_proxies(limit=3))
        self.assertEqual(result, [])

    def test_validation_deadline_returns_empty(self):
        async def fake_fetch_sources():
            return [("socks5", "1.1.1.1", 1080)]

        async def slow_validate(items, limit=10, concurrency=20):
            await asyncio.sleep(5)
            return []

        with (
            mock.patch.object(proxies, "fetch_sources", fake_fetch_sources),
            mock.patch.object(proxies, "validate_many", slow_validate),
        ):
            result = asyncio.run(proxies.get_working_proxies(limit=3, deadline=0.2))
        self.assertEqual(result, [])

    def test_candidates_deadline_keeps_manual_proxy(self):
        async def slow_get_working(limit=10, prefer_protocol="socks5", deadline=180.0):
            await asyncio.sleep(5)
            return []

        with (
            mock.patch.dict(
                os.environ,
                {
                    "PROXY_ENABLED": "1",
                    "PROXY_HOST": "1.2.3.4",
                    "PROXY_PORT": "1080",
                    "PROXY_TYPE": "http",
                    "PROXY_AUTO": "1",
                },
            ),
            mock.patch.object(proxies, "get_working_proxies", slow_get_working),
        ):
            result = asyncio.run(proxies.get_proxy_candidates(limit=5, deadline=0.2))
        self.assertEqual(
            result, [{"proxy_type": "http", "addr": "1.2.3.4", "port": 1080}]
        )


class StateRoundtripTest(BotTestCase):
    def test_save_load_roundtrip(self):
        userbot.model_overrides = {123: "model-a", -456: "model-b"}
        userbot.coder_chats = {1, -2}
        userbot.reasoning_hidden = {-100}
        userbot.tools_hidden = {777}
        userbot.save_state()
        userbot.model_overrides = {}
        userbot.coder_chats = set()
        userbot.reasoning_hidden = set()
        userbot.tools_hidden = set()
        userbot.load_state()
        self.assertEqual(userbot.model_overrides, {123: "model-a", -456: "model-b"})
        self.assertEqual(userbot.coder_chats, {1, -2})
        self.assertEqual(userbot.reasoning_hidden, {-100})
        self.assertEqual(userbot.tools_hidden, {777})

    def test_corrupt_json_tolerated(self):
        userbot.STATE_FILE.write_text("{not json", encoding="utf-8")
        userbot.model_overrides = {}
        userbot.coder_chats = set()
        userbot.load_state()
        self.assertEqual(userbot.model_overrides, {})
        self.assertEqual(userbot.coder_chats, set())

    def test_wrong_structure_tolerated(self):
        userbot.STATE_FILE.write_text('{"model_overrides": [1, 2]}', encoding="utf-8")
        userbot.model_overrides = {}
        userbot.load_state()
        self.assertEqual(userbot.model_overrides, {})


class HistoryRoundtripTest(BotTestCase):
    def test_roundtrip_and_maxlen(self):
        dm_msgs = [{"role": "user", "content": f"m{i}"} for i in range(3)]
        group_msgs = [{"role": "assistant", "content": "g"}]
        userbot.chat_history = {
            111: deque(dm_msgs, maxlen=userbot.DM_HISTORY_LIMIT),
            -222: deque(group_msgs, maxlen=userbot.GROUP_HISTORY_LIMIT),
        }
        userbot.save_history()
        userbot.chat_history = {}
        userbot.load_history()
        self.assertEqual(list(userbot.chat_history[111]), dm_msgs)
        self.assertEqual(userbot.chat_history[111].maxlen, userbot.DM_HISTORY_LIMIT)
        self.assertEqual(list(userbot.chat_history[-222]), group_msgs)
        self.assertEqual(userbot.chat_history[-222].maxlen, userbot.GROUP_HISTORY_LIMIT)

    def test_bad_keys_skipped(self):
        userbot.HISTORY_FILE.write_text(
            json.dumps({"abc": [], "111": [{"role": "user", "content": "x"}]}),
            encoding="utf-8",
        )
        userbot.load_history()
        self.assertNotIn("abc", {str(k) for k in userbot.chat_history})
        self.assertIn(111, userbot.chat_history)

    def test_corrupt_tolerated(self):
        userbot.HISTORY_FILE.write_text("[[[broken", encoding="utf-8")
        userbot.chat_history = {}
        userbot.load_history()
        self.assertEqual(userbot.chat_history, {})

    def test_non_list_values_skipped(self):
        userbot.HISTORY_FILE.write_text(
            json.dumps(
                {
                    "1": 5,
                    "2": "abc",
                    "3": [{"role": "user", "content": "x"}],
                    "4": None,
                }
            ),
            encoding="utf-8",
        )
        userbot.chat_history = {}
        userbot.load_history()
        self.assertNotIn(1, userbot.chat_history)
        self.assertNotIn(2, userbot.chat_history)
        self.assertNotIn(4, userbot.chat_history)
        self.assertEqual(
            list(userbot.chat_history[3]), [{"role": "user", "content": "x"}]
        )


class ModelsTextTest(BotTestCase):
    def test_default_model_marked_once(self):
        userbot.MODELS = ["deepseek-v4-flash"]
        userbot.model_overrides = {}
        text = userbot.models_text(-100)
        self.assertEqual(text.count("(текущая)"), 1)
        self.assertIn("deepseek-v4-flash", text)

    def test_override_appended(self):
        userbot.MODELS = ["base-model"]
        userbot.model_overrides = {-100: "custom-model"}
        text = userbot.models_text(-100)
        self.assertEqual(text.count("(текущая)"), 1)
        self.assertIn("custom-model (текущая)", text)
        self.assertIn("• base-model\n", text)


class RenderResponseTest(BotTestCase):
    def test_all_sections_present(self):
        text = userbot.render_response(
            "prefix\n\n",
            ["думаю", " ", "дальше"],
            ["web_search", "web_search", "fetch_url"],
            "ответ текста",
        )
        self.assertIn("reasoning:\nдумаю дальше", text)
        self.assertIn("tools: web_search, fetch_url", text)
        self.assertIn("ответ текста", text)
        self.assertEqual(text.count("web_search"), 1)

    def test_plain_text_output_has_no_markup(self):
        text = userbot.render_response("p\n\n", ["a<b"], ["x&y"], "c<d>e")
        self.assertIn("a<b", text)
        self.assertIn("x&y", text)
        self.assertIn("c<d>e", text)
        for banned in (
            "<i>",
            "<b>",
            "<code>",
            "\U0001f4ad",
            "\U0001f527",
            "\U0001f4ac",
        ):
            self.assertNotIn(banned, text)

    def test_hide_reasoning_and_tools(self):
        text = userbot.render_response(
            "p\n\n", ["думаю"], ["web_search"], "ответ", False, False
        )
        self.assertNotIn("думаю", text)
        self.assertNotIn("web_search", text)
        self.assertIn("ответ", text)

    def test_show_only_reasoning(self):
        text = userbot.render_response(
            "p\n\n", ["думаю"], ["web_search"], "ответ", True, False
        )
        self.assertIn("думаю", text)
        self.assertNotIn("web_search", text)

    def test_no_sections_when_empty(self):
        text = userbot.render_response("prefix:\n\n", [], [], "")
        self.assertEqual(text, "prefix:\n\n")


class ExecuteToolTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.fake_client = FakeClient()
        self._orig_client = userbot.client
        userbot.client = self.fake_client
        self.addCleanup(setattr, userbot, "client", self._orig_client)

    def test_evaluate(self):
        result = asyncio.run(
            userbot.execute_tool("evaluate", {"expression": "2+2"}, -100)
        )
        self.assertEqual(result, "4")

    def test_unknown_tool(self):
        result = asyncio.run(userbot.execute_tool("nope", {}, -100))
        self.assertEqual(result, "Неизвестная функция: nope")

    def test_get_chat_info_json(self):
        result = asyncio.run(userbot.execute_tool("get_chat_info", {}, -100))
        data = json.loads(result)
        self.assertEqual(data["id"], 7)
        self.assertEqual(data["username"], "uchat")
        self.assertEqual(data["members"], 11)

    def test_get_user_info(self):
        result = asyncio.run(
            userbot.execute_tool("get_user_info", {"handle": "@somebody"}, -100)
        )
        data = json.loads(result)
        self.assertEqual(data["name"], "A B")
        self.assertEqual(data["id"], 7)

    def test_get_user_info_empty_handle(self):
        result = asyncio.run(
            userbot.execute_tool("get_user_info", {"handle": ""}, -100)
        )
        self.assertEqual(result, "Пустой handle.")

    def test_get_profile_json(self):
        result = asyncio.run(userbot.execute_tool("get_profile", {}, -100))
        data = json.loads(result)
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["username"], "meuser")


class NewToolsTest(BotTestCase):
    CHAT_ID = -100

    def test_get_time(self):
        result = asyncio.run(userbot.execute_tool("get_time", {}, self.CHAT_ID))
        data = json.loads(result)
        self.assertIn("utc", data)
        self.assertIn("local", data)
        self.assertIn("weekday", data)

    def test_text_stats(self):
        result = asyncio.run(
            userbot.execute_tool("text_stats", {"text": "hi there\nbye"}, self.CHAT_ID)
        )
        data = json.loads(result)
        self.assertEqual(data["chars"], 12)
        self.assertEqual(data["words"], 3)
        self.assertEqual(data["lines"], 2)

    def test_get_bot_stats(self):
        result = asyncio.run(userbot.execute_tool("get_bot_stats", {}, self.CHAT_ID))
        data = json.loads(result)
        self.assertIn("uptime_seconds", data)
        self.assertIn("python", data)
        self.assertIn("modes", data)

    def test_run_shell_echo(self):
        result = _owner_tool("run_shell", {"command": "echo hello"}, self.CHAT_ID)
        self.assertIn("hello", result)

    def test_run_shell_empty(self):
        result = _owner_tool("run_shell", {"command": ""}, self.CHAT_ID)
        self.assertEqual(result, "Пустая команда.")


class BotCommandsTest(BotTestCase):
    CASES: ClassVar[list] = [
        ("/help", ("help", None)),
        ("/start", ("help", None)),
        ("/?", ("help", None)),
        ("/model gpt-x", ("model", "gpt-x")),
        ("/model", ("model", None)),
        ("/models", ("models", None)),
        ("/clear", ("clear", None)),
        ("/settings", ("settings", None)),
        ("/coder on", ("coder", True)),
        ("/coder off", ("coder", False)),
        ("/coder", ("coder_status", None)),
        ("/reasoning on", ("reasoning", True)),
        ("/tools off", ("tools", False)),
        ("/tools", ("tools_status", None)),
        ("/prompt", ("prompt", None)),
        ("/help@DanyBOTAPI_bot", ("help", None)),
        ("  /SETTINGS  ", ("settings", None)),
    ]

    NONE_CASES: ClassVar[list] = [
        "help",
        "/unknown",
        "/",
        "/ignore",
        "/unignore",
        "/ping",
        "/history",
        ".db ping",
    ]

    def test_bot_commands(self):
        for text, expected in self.CASES:
            self.assertEqual(bot.handle_bot_commands(text), expected)

    def test_bot_commands_none(self):
        for text in self.NONE_CASES:
            self.assertIsNone(bot.handle_bot_commands(text))


class SystemForModeTest(BotTestCase):
    def test_userbot_uses_system_prompt(self):
        self.assertIn("юзербот", userbot.system_for(-100, "userbot"))

    def test_bot_uses_bot_prompt(self):
        self.assertIn("бот", userbot.system_for(-100, "bot"))

    def test_default_mode_is_userbot(self):
        self.assertEqual(userbot.system_for(-100), userbot.system_for(-100, "userbot"))

    def test_tools_expose_tools_module(self):
        self.assertIs(userbot.TOOLS, tools_module.TOOLS)

    def test_tools_has_all_required(self):
        names = {t["function"]["name"] for t in userbot.TOOLS}
        for required in (
            "evaluate",
            "get_chat_info",
            "get_user_info",
            "get_profile",
            "run_shell",
            "web_search",
            "fetch_url",
            "run_subagent",
            "get_time",
            "text_stats",
            "get_bot_stats",
        ):
            self.assertIn(required, names)

    def test_tools_excludes_removed(self):
        names = {t["function"]["name"] for t in userbot.TOOLS}
        for forbidden in (
            "random_value",
            "hash_text",
            "base64_codec",
            "list_source_files",
            "read_source_file",
            "write_source_file",
        ):
            self.assertNotIn(forbidden, names)


class EnvBoolTest(BotTestCase):
    def test_default_when_unset(self):
        os.environ.pop("DANYBOT_TEST_BOOL", None)
        self.assertTrue(userbot._env_bool("DANYBOT_TEST_BOOL", True))
        self.assertFalse(userbot._env_bool("DANYBOT_TEST_BOOL", False))

    def test_false_values(self):
        for val in ("0", "false", "no", "off", "False", "NO", "OFF", " 0 "):
            with mock.patch.dict(os.environ, {"DANYBOT_TEST_BOOL": val}):
                self.assertFalse(userbot._env_bool("DANYBOT_TEST_BOOL", True))

    def test_true_values(self):
        for val in ("1", "true", "yes", "on", "TRUE", " Yes "):
            with mock.patch.dict(os.environ, {"DANYBOT_TEST_BOOL": val}):
                self.assertTrue(userbot._env_bool("DANYBOT_TEST_BOOL", False))


class ExecuteToolClientOverrideTest(BotTestCase):
    CHAT_ID = -100

    def test_override_client_used(self):
        class OverrideClient:
            def __init__(self):
                self.calls = []

            async def get_me(self):
                self.calls.append("get_me")
                return SimpleNamespace(
                    id=9,
                    first_name="B",
                    last_name="Bot",
                    username="botuser",
                )

        override = OverrideClient()
        result = asyncio.run(
            userbot.execute_tool("get_profile", {}, self.CHAT_ID, override)
        )
        data = json.loads(result)
        self.assertEqual(data["id"], 9)
        self.assertEqual(data["username"], "botuser")
        self.assertEqual(override.calls, ["get_me"])


class StreamToolsTest(BotTestCase):
    CHAT_ID = -100

    def setUp(self):
        super().setUp()
        self.tool_calls_made = []

        async def fake_execute(
            name,
            args,
            chat_id,
            client=None,
            stats=None,
            unrestricted=False,
            allowed=None,
        ):
            self.tool_calls_made.append((name, args, chat_id))
            return "TOOLOK"

        self._orig_execute = userbot.execute_tool
        self._orig_ai = userbot.ai
        userbot.execute_tool = fake_execute
        self.addCleanup(setattr, userbot, "execute_tool", self._orig_execute)
        self.addCleanup(setattr, userbot, "ai", self._orig_ai)

    def install_ai(self, rounds):
        fake_ai = FakeAI(rounds)
        userbot.ai = fake_ai
        return fake_ai

    @staticmethod
    async def collect(parts, part):
        parts.append(part)

    def test_plain_content_stream(self):
        fake_ai = self.install_ai(
            [
                [
                    make_chunk(make_delta(content="Hi")),
                    make_chunk(make_delta(content="!")),
                ]
            ]
        )
        deltas = []
        answer = asyncio.run(
            userbot.stream_with_tools(
                [{"role": "user", "content": "q"}],
                "deepseek-v4-flash",
                self.CHAT_ID,
                lambda p: self.collect(deltas, p),
                lambda p: self.collect([], p),
            )
        )
        self.assertEqual(answer, "Hi!")
        self.assertEqual(deltas, ["Hi", "!"])
        self.assertEqual(self.tool_calls_made, [])
        kwargs = fake_ai.chat.completions.calls[0]
        self.assertEqual(kwargs["model"], "deepseek-v4-flash")
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["tools"], userbot.TOOLS)
        self.assertEqual(kwargs["messages"][0]["role"], "user")

    def test_reasoning_content_collected(self):
        self.install_ai(
            [
                [
                    make_chunk(make_delta(reasoning_content="думаю")),
                    make_chunk(make_delta(content="ответ")),
                ]
            ]
        )
        reasons = []
        answer = asyncio.run(
            userbot.stream_with_tools(
                [],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect(reasons, p),
            )
        )
        self.assertEqual(answer, "ответ")
        self.assertEqual(reasons, ["думаю"])

    def test_tool_call_roundtrip(self):
        fake_ai = self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(tool_calls=[make_tc(tc_id="call1", name="evaluate")])
                    ),
                    make_chunk(
                        make_delta(
                            tool_calls=[make_tc(arguments='{"expression": "2+3"}')]
                        )
                    ),
                ],
                [make_chunk(make_delta(content="Итог: 5"))],
            ]
        )
        answer = asyncio.run(
            userbot.stream_with_tools(
                [{"role": "user", "content": "посчитай"}],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
            )
        )
        self.assertEqual(answer, "Итог: 5")
        self.assertEqual(
            self.tool_calls_made,
            [("evaluate", {"expression": "2+3"}, self.CHAT_ID)],
        )
        second_messages = fake_ai.chat.completions.calls[1]["messages"]
        roles = [m["role"] for m in second_messages]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        assistant_msg = second_messages[1]
        self.assertEqual(len(assistant_msg["tool_calls"]), 1)
        self.assertEqual(assistant_msg["tool_calls"][0]["function"]["name"], "evaluate")
        self.assertEqual(second_messages[2]["content"], "TOOLOK")
        self.assertEqual(second_messages[2]["tool_call_id"], "call1")

    def test_repeated_tool_name_is_not_duplicated(self):
        fake_ai = self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(tool_calls=[make_tc(tc_id="c1", name="evaluate")])
                    ),
                    make_chunk(make_delta(tool_calls=[make_tc(name="evaluate")])),
                    make_chunk(
                        make_delta(
                            tool_calls=[make_tc(arguments='{"expression": "2+3"}')]
                        )
                    ),
                ],
                [make_chunk(make_delta(content="Итог: 5"))],
            ]
        )
        answer = asyncio.run(
            userbot.stream_with_tools(
                [{"role": "user", "content": "посчитай"}],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
            )
        )
        self.assertEqual(answer, "Итог: 5")
        self.assertEqual(
            self.tool_calls_made,
            [("evaluate", {"expression": "2+3"}, self.CHAT_ID)],
        )
        assistant_msg = fake_ai.chat.completions.calls[1]["messages"][1]
        self.assertEqual(assistant_msg["tool_calls"][0]["function"]["name"], "evaluate")

    def test_missing_tool_call_id_is_synthesized(self):
        fake_ai = self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(
                            tool_calls=[
                                make_tc(
                                    name="evaluate", arguments='{"expression": "1"}'
                                )
                            ]
                        )
                    )
                ],
                [make_chunk(make_delta(content="ok"))],
            ]
        )
        asyncio.run(
            userbot.stream_with_tools(
                [{"role": "user", "content": "посчитай"}],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
            )
        )
        second = fake_ai.chat.completions.calls[1]["messages"]
        assistant_id = second[1]["tool_calls"][0]["id"]
        self.assertTrue(assistant_id)
        self.assertEqual(second[2]["tool_call_id"], assistant_id)

    def test_round_limit_stops_tool_loop(self):
        saved_limit = userbot.TOOL_MAX_ROUNDS
        self.addCleanup(setattr, userbot, "TOOL_MAX_ROUNDS", saved_limit)
        userbot.TOOL_MAX_ROUNDS = 2
        rounds = [
            [
                make_chunk(
                    make_delta(
                        tool_calls=[
                            make_tc(
                                tc_id="c1",
                                name="evaluate",
                                arguments='{"expression": "1"}',
                            )
                        ]
                    )
                )
            ]
            for _ in range(10)
        ]
        fake_ai = self.install_ai(rounds)
        reported = []

        def on_progress(count, calls, reason=""):
            reported.append((count, calls, reason))

        answer = asyncio.run(
            userbot.stream_with_tools(
                [{"role": "user", "content": "зациклись"}],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
                on_progress=on_progress,
            )
        )
        self.assertEqual(len(fake_ai.chat.completions.calls), 2)
        self.assertIn("лимит раундов инструментов", answer)
        self.assertTrue(reported[-1][2])

    def test_tool_callback_receives_call_and_result(self):
        self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(tool_calls=[make_tc(tc_id="call1", name="evaluate")])
                    ),
                    make_chunk(
                        make_delta(
                            tool_calls=[make_tc(arguments='{"expression": "2+3"}')]
                        )
                    ),
                ],
                [make_chunk(make_delta(content="Итог: 5"))],
            ]
        )
        tools_seen = []

        async def on_tool(name):
            tools_seen.append(name)

        answer = asyncio.run(
            userbot.stream_with_tools(
                [{"role": "user", "content": "посчитай"}],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
                on_tool,
            )
        )
        self.assertEqual(answer, "Итог: 5")
        self.assertEqual(tools_seen, ["evaluate"])

    def test_denied_tool_is_not_reported(self):
        self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(
                            tool_calls=[make_tc(tc_id="call1", name="run_shell")]
                        )
                    ),
                    make_chunk(
                        make_delta(
                            tool_calls=[make_tc(arguments='{"command": "rm -rf /"}')]
                        )
                    ),
                ],
                [make_chunk(make_delta(content="отказ"))],
            ]
        )
        tools_seen = []

        async def deny(name, args, model, unrestricted=False):
            return False

        async def on_tool(name):
            tools_seen.append(name)

        with mock.patch.object(userbot, "verify_tool_call", deny):
            answer = asyncio.run(
                userbot.stream_with_tools(
                    [{"role": "user", "content": "q"}],
                    "m",
                    self.CHAT_ID,
                    lambda p: self.collect([], p),
                    lambda p: self.collect([], p),
                    on_tool,
                    tools=userbot.TOOLS,
                    verify_tools=True,
                )
            )
        self.assertEqual(answer, "отказ")
        self.assertEqual(tools_seen, [])
        self.assertEqual(self.tool_calls_made, [])

    def test_broken_tool_arguments_are_reported(self):
        fake_ai = self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(
                            tool_calls=[
                                make_tc(
                                    tc_id="c1", name="evaluate", arguments="{not json"
                                )
                            ]
                        )
                    )
                ],
                [make_chunk(make_delta(content="починено"))],
            ]
        )
        answer = asyncio.run(
            userbot.stream_with_tools(
                [{"role": "user", "content": "q"}],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
            )
        )
        self.assertEqual(answer, "починено")
        self.assertEqual(self.tool_calls_made, [])
        second = fake_ai.chat.completions.calls[1]["messages"]
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        self.assertTrue(tool_msgs)
        self.assertIn("Некорректный JSON", tool_msgs[0]["content"])

    def test_non_object_tool_arguments_are_reported(self):
        fake_ai = self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(
                            tool_calls=[
                                make_tc(tc_id="c1", name="evaluate", arguments="[1]")
                            ]
                        )
                    )
                ],
                [make_chunk(make_delta(content="ок"))],
            ]
        )
        asyncio.run(
            userbot.stream_with_tools(
                [{"role": "user", "content": "q"}],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
            )
        )
        self.assertEqual(self.tool_calls_made, [])
        second = fake_ai.chat.completions.calls[1]["messages"]
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        self.assertIn("объектом JSON", tool_msgs[0]["content"])

    def test_subagent_output_is_sanitized(self):
        self.assertIn("run_subagent", userbot.SANITIZED_TOOLS)

    def test_approved_tool_is_reported_before_result(self):
        self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(
                            tool_calls=[
                                make_tc(tc_id="c1", name="evaluate", arguments="{}")
                            ]
                        )
                    )
                ],
                [make_chunk(make_delta(content="готово"))],
            ]
        )
        seen = []

        async def on_tool(name):
            seen.append(name)

        async def allow(name, args, model, unrestricted=False):
            return True

        with mock.patch.object(userbot, "verify_tool_call", allow):
            asyncio.run(
                userbot.stream_with_tools(
                    [{"role": "user", "content": "q"}],
                    "m",
                    self.CHAT_ID,
                    lambda p: self.collect([], p),
                    lambda p: self.collect([], p),
                    on_tool,
                    tools=userbot.TOOLS,
                    verify_tools=True,
                )
            )
        self.assertEqual(seen, ["evaluate"])

    def test_loop_has_no_round_limit(self):
        endless = [
            make_chunk(make_delta(tool_calls=[make_tc(tc_id="c1", name="evaluate")]))
        ]
        rounds = 25
        self.install_ai(
            [endless] * rounds + [[make_chunk(make_delta(content="готово"))]]
        )
        seen = []
        answer = asyncio.run(
            userbot.stream_with_tools(
                [],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
                on_progress=lambda r, t, reason="": seen.append((r, t, reason)),
            )
        )
        self.assertEqual(answer, "готово")
        self.assertEqual(len(self.tool_calls_made), rounds)
        self.assertEqual(seen[-1][:2], (rounds + 1, rounds))
        self.assertTrue(all(not reason for _r, _t, reason in seen))

    def test_repeated_call_is_executed_every_time(self):
        endless = [
            make_chunk(
                make_delta(
                    tool_calls=[
                        make_tc(
                            tc_id="c1",
                            name="evaluate",
                            arguments='{"expression":"1+1"}',
                        )
                    ]
                )
            )
        ]
        self.install_ai([endless] * 4 + [[make_chunk(make_delta(content="стоп"))]])
        answer = asyncio.run(
            userbot.stream_with_tools(
                [],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
            )
        )
        self.assertEqual(answer, "стоп")
        self.assertEqual(len(self.tool_calls_made), 4)

    def test_denied_call_is_reported_and_loop_continues(self):
        endless = [
            make_chunk(
                make_delta(
                    tool_calls=[
                        make_tc(
                            tc_id="c1",
                            name="run_shell",
                            arguments='{"command":"echo hi"}',
                        )
                    ]
                )
            )
        ]
        fake_ai = self.install_ai(
            [endless] * 3 + [[make_chunk(make_delta(content="ок"))]]
        )

        async def deny(name, args, model, unrestricted=False):
            return False

        with mock.patch.object(userbot, "verify_tool_call", deny):
            answer = asyncio.run(
                userbot.stream_with_tools(
                    [],
                    "m",
                    self.CHAT_ID,
                    lambda p: self.collect([], p),
                    lambda p: self.collect([], p),
                    verify_tools=True,
                )
            )
        self.assertEqual(answer, "ок")
        self.assertEqual(self.tool_calls_made, [])
        final = fake_ai.chat.completions.calls[-1]["messages"]
        denials = [
            m["content"]
            for m in final
            if m.get("role") == "tool" and "отклонён" in m["content"]
        ]
        self.assertEqual(len(denials), 3)

    def test_failing_tool_is_reported_without_stopping_immediately(self):
        endless = [
            make_chunk(make_delta(tool_calls=[make_tc(tc_id="c1", name="evaluate")]))
        ]
        self.install_ai([endless, [make_chunk(make_delta(content="всё"))]])

        async def boom(name, args, chat_id, *rest, **kwargs):
            raise RuntimeError("tool down")

        saved = userbot.execute_tool
        userbot.execute_tool = boom
        self.addCleanup(setattr, userbot, "execute_tool", saved)
        answer = asyncio.run(
            userbot.stream_with_tools(
                [],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
            )
        )
        self.assertEqual(answer, "всё")

    def test_progress_callback_reports_counters(self):
        endless = [
            make_chunk(make_delta(tool_calls=[make_tc(tc_id="c1", name="evaluate")]))
        ]
        self.install_ai([endless] * 3 + [[make_chunk(make_delta(content="ок"))]])
        seen = []
        answer = asyncio.run(
            userbot.stream_with_tools(
                [],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
                on_progress=lambda rounds, tools, reason="": seen.append(
                    (rounds, tools, reason)
                ),
            )
        )
        self.assertEqual(answer, "ок")
        self.assertEqual(seen[0], (1, 1, ""))
        self.assertEqual(seen[-1][:2], (4, 3))

    def test_progress_callback_failure_is_ignored(self):
        def boom(_rounds, _tools, _reason=""):
            raise RuntimeError("journal down")

        fake_ai = self.install_ai([[make_chunk(make_delta(content="ок"))]])
        answer = asyncio.run(
            userbot.stream_with_tools(
                [],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
                on_progress=boom,
            )
        )
        self.assertEqual(answer, "ок")
        self.assertTrue(fake_ai.chat.completions.calls)

    def test_loop_budget_knobs_are_gone(self):
        for name in (
            "MAX_TOOL_ROUNDS",
            "MAX_TOOL_SECONDS",
            "MAX_TOOL_STUCK",
            "MAX_TOOL_REPEATS",
            "MAX_TOOL_CALLS_PER_ROUND",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(userbot, name))
        for name in ("_loop_stop_reason", "_finish_loop"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(userbot, name))

    def test_unrestricted_request_skips_verification(self):
        self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(tool_calls=[make_tc(tc_id="c1", name="evaluate")])
                    ),
                    make_chunk(
                        make_delta(
                            tool_calls=[make_tc(arguments='{"expression": "2+3"}')]
                        )
                    ),
                ],
                [make_chunk(make_delta(content="готово"))],
            ]
        )
        verify_calls = []

        async def verify(name, args, model, unrestricted=False):
            verify_calls.append(name)
            return False

        with mock.patch.object(userbot, "verify_tool_call", verify):
            answer = asyncio.run(
                userbot.stream_with_tools(
                    [{"role": "user", "content": "q"}],
                    "m",
                    self.CHAT_ID,
                    lambda p: self.collect([], p),
                    lambda p: self.collect([], p),
                    tools=userbot.TOOLS,
                    verify_tools=True,
                    unrestricted=True,
                )
            )
        self.assertEqual(answer, "готово")
        self.assertEqual(verify_calls, [])
        self.assertEqual(len(self.tool_calls_made), 1)

    def test_answer_keeps_text_from_every_round(self):
        self.install_ai(
            [
                [
                    make_chunk(make_delta(content="первый ")),
                    make_chunk(
                        make_delta(tool_calls=[make_tc(tc_id="c1", name="evaluate")])
                    ),
                    make_chunk(
                        make_delta(
                            tool_calls=[make_tc(arguments='{"expression": "2+3"}')]
                        )
                    ),
                ],
                [make_chunk(make_delta(content="второй"))],
            ]
        )
        answer = asyncio.run(
            userbot.stream_with_tools(
                [{"role": "user", "content": "q"}],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
            )
        )
        self.assertEqual(answer, "первый второй")

    def test_stream_is_closed_after_completion(self):
        fake_ai = self.install_ai([[make_chunk(make_delta(content="ok"))]])
        asyncio.run(
            userbot.stream_with_tools(
                [],
                "m",
                self.CHAT_ID,
                lambda p: self.collect([], p),
                lambda p: self.collect([], p),
            )
        )
        streams = fake_ai.chat.completions.streams
        self.assertTrue(streams)
        self.assertTrue(all(s.closed >= 1 for s in streams))

    def test_stream_is_closed_after_error(self):
        fake_ai = self.install_ai([[make_chunk(make_delta(content="ok"))]])

        async def boom(_p):
            raise RuntimeError("edit failed")

        with self.assertRaises(RuntimeError):
            asyncio.run(
                userbot.stream_with_tools(
                    [],
                    "m",
                    self.CHAT_ID,
                    boom,
                    lambda p: self.collect([], p),
                )
            )
        streams = fake_ai.chat.completions.streams
        self.assertTrue(all(s.closed >= 1 for s in streams))

    def test_chunk_without_choices_is_skipped(self):
        fake_ai = self.install_ai(
            [
                [
                    SimpleNamespace(choices=[]),
                    make_chunk(make_delta(content="ок")),
                ]
            ]
        )
        deltas = []
        answer = asyncio.run(
            userbot.stream_with_tools(
                [],
                "m",
                self.CHAT_ID,
                lambda p: self.collect(deltas, p),
                lambda p: self.collect([], p),
            )
        )
        self.assertEqual(answer, "ок")
        self.assertEqual(deltas, ["ок"])
        self.assertTrue(all(s.closed >= 1 for s in fake_ai.chat.completions.streams))

    def test_shell_output_is_passed_through_sanitizer(self):
        fake_ai = self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(
                            tool_calls=[
                                make_tc(
                                    tc_id="c1",
                                    name="run_shell",
                                    arguments='{"command": "echo hi"}',
                                )
                            ]
                        )
                    )
                ],
                [make_chunk(make_delta(content="готово"))],
            ]
        )
        seen = []

        async def sanitize(output, model, unrestricted=False):
            seen.append((output, model))
            return "чистый вывод"

        with mock.patch.object(userbot, "sanitize_tool_output", sanitize):
            answer = asyncio.run(
                userbot.stream_with_tools(
                    [],
                    "m",
                    self.CHAT_ID,
                    lambda p: self.collect([], p),
                    lambda p: self.collect([], p),
                )
            )
        self.assertEqual(answer, "готово")
        self.assertEqual(seen, [("TOOLOK", "m")])
        second = fake_ai.chat.completions.calls[1]["messages"]
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        self.assertEqual(tool_msgs[0]["content"], "чистый вывод")

    def test_cancelled_tool_call_is_reraised(self):
        self.install_ai(
            [
                [
                    make_chunk(
                        make_delta(
                            tool_calls=[
                                make_tc(tc_id="c1", name="evaluate", arguments="{}")
                            ]
                        )
                    )
                ],
                [make_chunk(make_delta(content="не дойдёт"))],
            ]
        )

        async def boom(name, args, chat_id, *rest, **kwargs):
            raise asyncio.CancelledError

        saved = userbot.execute_tool
        userbot.execute_tool = boom
        self.addCleanup(setattr, userbot, "execute_tool", saved)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(
                userbot.stream_with_tools(
                    [],
                    "m",
                    self.CHAT_ID,
                    lambda p: self.collect([], p),
                    lambda p: self.collect([], p),
                )
            )


class LastDecisionTest(BotTestCase):
    def test_empty_text(self):
        self.assertEqual(userbot._last_decision(""), "")
        self.assertEqual(userbot._last_decision("   \n  "), "")

    def test_no_keywords(self):
        self.assertEqual(userbot._last_decision("просто текст"), "")
        self.assertEqual(userbot._last_decision("думаю и рассуждаю"), "")

    def test_case_insensitive(self):
        self.assertEqual(userbot._last_decision("allow"), "ALLOW")
        self.assertEqual(userbot._last_decision("deny"), "DENY")

    def test_verdict_on_last_line(self):
        self.assertEqual(userbot._last_decision("рассуждение\nALLOW"), "ALLOW")
        self.assertEqual(userbot._last_decision("Шаг 1\nШаг 2\nDENY"), "DENY")

    def test_last_line_wins_over_earlier(self):
        self.assertEqual(userbot._last_decision("DENY\nALLOW"), "ALLOW")
        self.assertEqual(userbot._last_decision("ALLOW\nDENY"), "DENY")

    def test_verdict_inline_colon(self):
        self.assertEqual(userbot._last_decision("Вердикт: ALLOW"), "ALLOW")
        self.assertEqual(userbot._last_decision("Ответ: DENY"), "DENY")

    def test_whitespace_lines_skipped(self):
        self.assertEqual(userbot._last_decision("ALLOW\n\n  \n"), "ALLOW")

    def test_word_within_word_not_matched(self):
        self.assertEqual(userbot._last_decision("allowed"), "")
        self.assertEqual(userbot._last_decision("denyable"), "")


class NonStreamMessage:
    def __init__(self, content=None, reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content


class NonStreamResponse:
    def __init__(self, message):
        self.choices = [SimpleNamespace(message=message)]


class NonStreamCompletions:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


class NonStreamAI:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=NonStreamCompletions(response))


class VerifyToolCallTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self._orig_ai = userbot.ai
        self.addCleanup(setattr, userbot, "ai", self._orig_ai)

    def install_ai(self, response):
        fake_ai = NonStreamAI(response)
        userbot.ai = fake_ai
        return fake_ai

    def test_allows_on_content_allow(self):
        fake_ai = self.install_ai(NonStreamResponse(NonStreamMessage(content="ALLOW")))
        ok = asyncio.run(userbot.verify_tool_call("run_shell", {"command": "ls"}, "m"))
        self.assertTrue(ok)
        self.assertEqual(
            fake_ai.chat.completions.calls[0]["model"], userbot.RUN_SHELL_MODEL or "m"
        )
        self.assertFalse(fake_ai.chat.completions.calls[0]["stream"])

    def test_rejects_deny(self):
        self.install_ai(NonStreamResponse(NonStreamMessage(content="DENY")))
        ok = asyncio.run(
            userbot.verify_tool_call("run_shell", {"command": "rm -rf /"}, "m")
        )
        self.assertFalse(ok)

    def test_allows_reasoning_content_only(self):
        self.install_ai(
            NonStreamResponse(
                NonStreamMessage(
                    content="", reasoning_content="команда безопасна\nALLOW"
                )
            )
        )
        ok = asyncio.run(userbot.verify_tool_call("run_shell", {"command": "ls"}, "m"))
        self.assertTrue(ok)

    def test_rejects_reasoning_content_deny(self):
        self.install_ai(
            NonStreamResponse(
                NonStreamMessage(content="", reasoning_content="опасно\nDENY")
            )
        )
        ok = asyncio.run(
            userbot.verify_tool_call("run_shell", {"command": "rm -rf /"}, "m")
        )
        self.assertFalse(ok)

    def test_rejects_last_line_deny_after_allow(self):
        self.install_ai(NonStreamResponse(NonStreamMessage(content="ALLOW\nDENY")))
        ok = asyncio.run(userbot.verify_tool_call("run_shell", {"command": "x"}, "m"))
        self.assertFalse(ok)

    def test_rejects_empty_response(self):
        self.install_ai(NonStreamResponse(NonStreamMessage(content=None)))
        ok = asyncio.run(userbot.verify_tool_call("run_shell", {"command": "x"}, "m"))
        self.assertFalse(ok)

    def test_rejects_empty_reasoning_only(self):
        self.install_ai(
            NonStreamResponse(
                NonStreamMessage(content="", reasoning_content="никакого решения")
            )
        )
        ok = asyncio.run(userbot.verify_tool_call("run_shell", {"command": "x"}, "m"))
        self.assertFalse(ok)

    def test_handles_list_content(self):
        self.install_ai(NonStreamResponse(NonStreamMessage(content=["AL", "LOW"])))
        ok = asyncio.run(userbot.verify_tool_call("run_shell", {"command": "x"}, "m"))
        self.assertTrue(ok)

    def test_api_error_returns_false(self):
        self.install_ai(ValueError("boom"))
        ok = asyncio.run(userbot.verify_tool_call("run_shell", {"command": "x"}, "m"))
        self.assertFalse(ok)

    def test_missing_choices_returns_false(self):
        self.install_ai(SimpleNamespace(choices=[]))
        ok = asyncio.run(userbot.verify_tool_call("run_shell", {"command": "x"}, "m"))
        self.assertFalse(ok)

    def test_verifier_budget_fits_a_verdict(self):
        fake_ai = self.install_ai(NonStreamResponse(NonStreamMessage(content="ALLOW")))
        ok = asyncio.run(userbot.verify_tool_call("fetch_url", {}, "m"))
        self.assertTrue(ok)
        self.assertGreaterEqual(
            fake_ai.chat.completions.calls[0]["max_tokens"],
            userbot.VERIFY_TOKENS_TOOL,
        )
        self.assertGreaterEqual(userbot.VERIFY_TOKENS_TOOL, 256)

    def test_verifier_budget_for_shell(self):
        fake_ai = self.install_ai(NonStreamResponse(NonStreamMessage(content="ALLOW")))
        asyncio.run(userbot.verify_tool_call("run_shell", {"command": "ls"}, "m"))
        self.assertEqual(
            fake_ai.chat.completions.calls[0]["max_tokens"],
            userbot.VERIFY_TOKENS_SHELL,
        )


class FloodRetryTest(BotTestCase):
    def make_event(self, script):
        attempts = {"n": 0}

        class FakeEvent:
            chat_id = 4242

            async def reply(self, text):
                attempts["n"] += 1
                behavior = script[min(attempts["n"], len(script)) - 1]
                if behavior == "flood":
                    raise FloodWaitError(request=None)
                if behavior == "rpc":
                    raise RPCError(None, "boom", 400)
                return SimpleNamespace(id=777)

        return FakeEvent(), attempts

    def test_safe_reply_success_after_floods(self):
        event, attempts = self.make_event(["flood", "flood", "ok"])
        sent = asyncio.run(userbot.safe_reply(event, "текст"))
        if sent is None or sent.id != 777:
            self.fail("safe_reply должен вернуть сообщение с id 777")
        self.assertEqual(attempts["n"], 3)
        self.assertIn((4242, 777), userbot.recent_reply_ids)

    def test_safe_reply_bounded_attempts(self):
        event, attempts = self.make_event(["flood"])
        sent = asyncio.run(userbot.safe_reply(event, "текст"))
        self.assertIsNone(sent)
        self.assertEqual(attempts["n"], userbot.REPLY_ATTEMPTS)

    def test_safe_reply_rpc_fail_fast(self):
        event, attempts = self.make_event(["rpc"])
        sent = asyncio.run(userbot.safe_reply(event, "текст"))
        self.assertIsNone(sent)
        self.assertEqual(attempts["n"], 1)


class EditRetryTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.flaky = FlakyEditClient()
        self._orig_client = userbot.client
        userbot.client = self.flaky
        self.addCleanup(setattr, userbot, "client", self._orig_client)

    def test_edit_text_recovers_after_floods(self):
        ok = asyncio.run(userbot.edit_text(-100, 5, "новый текст"))
        self.assertTrue(ok)
        self.assertEqual(self.flaky.attempts, 3)

    def test_edit_text_bounded(self):
        self.flaky.always_flood = True
        ok = asyncio.run(userbot.edit_text(-100, 5, "новый текст"))
        self.assertFalse(ok)
        self.assertEqual(self.flaky.attempts, userbot.REPLY_ATTEMPTS)


class FlakyEditClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.attempts = 0
        self.always_flood = False

    async def edit_message(self, chat, msg_id, text, **kwargs):
        self.attempts += 1
        if self.always_flood or self.attempts < 3:
            raise FloodWaitError(request=None)
        return True


class RichFakeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace()


class _FakeResp:
    def __init__(self, text, status=200, location=None):
        self.text = text
        self.status_code = status
        self.headers = {"location": location} if location else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError("bad status")

    async def aiter_bytes(self):
        raw = self.text.encode("utf-8")
        for start in range(0, len(raw), 4096):
            yield raw[start : start + 4096]


def broken_async_exec(proc):
    async def create(*_args, **_kwargs):
        return proc

    return create


def scan_files(root, glob_pat, pattern, limit, stop=None):
    return tools_module._scan_files(
        Path(root),
        glob_pat,
        re.compile(pattern),
        limit,
        stop or threading.Event(),
    )


class _FakeStdin:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def read(self):
        return self._payload

    def reconfigure(self, **_kwargs):
        self.calls += 1


class _FakeStdout:
    def __init__(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)

    def reconfigure(self, **_kwargs):
        return None


def _run_scan_worker(request):
    fake_out = _FakeStdout()
    with (
        mock.patch.object(sys, "stdin", _FakeStdin(request)),
        mock.patch.object(sys, "stdout", fake_out),
    ):
        tools_module._scan_worker_main()
    return json.loads("".join(fake_out.chunks))


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *_):
        return False


class _FakeHttpx:
    def __init__(self, text, status=200):
        self._text = text
        self._status = status

    def _resp(self):
        return _FakeResp(self._text, self._status)

    async def get(self, url, **kwargs):
        return self._resp()

    def stream(self, _method, url, **kwargs):
        return _FakeStream(self._resp())


class _FakeReplyEvent:
    chat_id = 5

    def __init__(self, fail_times=0):
        self.replies = []
        self._fail = fail_times

    async def reply(self, text):
        if self._fail > 0:
            self._fail -= 1
            raise OSError("boom")
        self.replies.append(text)
        return SimpleNamespace(id=77)


class _StoreStub:
    def __init__(self):
        self.recent_reply_ids = set()
        self.ctx_lock = asyncio.Lock()
        self.chat_history = {}
        self.model_overrides = {}
        self.coder_chats = set()
        self.reasoning_hidden = set()
        self.tools_hidden = set()
        self.seen_msg_keys = set()
        self.last_chat_activity = {}


def _task_outcome(task):
    if not task.done():
        return "pending"
    if task.cancelled():
        return "cancelled"
    return repr(task.exception())


async def _wait_done(task, attempts=50):
    for _ in range(attempts):
        if task.done():
            return True
        await asyncio.sleep(0.005)
    return task.done()


def _owner_tool(name, arguments, chat_id=-100, **kwargs):
    return asyncio.run(
        userbot.execute_tool(name, arguments, chat_id, unrestricted=True, **kwargs)
    )


class ExtraToolsTest(BotTestCase):
    CHAT_ID = -100

    def setUp(self):
        super().setUp()
        self.fake_client = RichFakeClient()
        self._orig_client = userbot.client
        userbot.client = self.fake_client
        self.addCleanup(setattr, userbot, "client", self._orig_client)

    def _run(self, name, args):
        return asyncio.run(userbot.execute_tool(name, args, self.CHAT_ID))

    def _owner_run(self, name, args):
        return _owner_tool(name, args, self.CHAT_ID)

    def test_web_search_empty_and_ok(self):
        self.assertEqual(self._run("web_search", {"query": ""}), "Пустой запрос.")
        html = '<a class="result__a" href="https://ex.com">Title</a>'
        with mock.patch.object(
            tools_module, "_get_httpx_client", lambda: _FakeHttpx(html)
        ):
            out = self._run("web_search", {"query": "x"})
        self.assertIn("Title", out)
        self.assertIn("https://ex.com", out)

    def test_web_search_brave_html(self):
        html = (
            '<div class="snippet svelte-x" data-pos="0" data-type="web">'
            '<a href="https://docs.python.org/3/whatsnew/3.14.html">'
            '<div class="title search-snippet-title">Python 3.14 docs</div></a>'
            '<div class="generic-snippet">Release highlights</div></div>'
        )
        with mock.patch.object(
            tools_module, "_get_httpx_client", lambda: _FakeHttpx(html)
        ):
            out = self._run("web_search", {"query": "python 3.14"})
        self.assertIn("Python 3.14 docs", out)
        self.assertIn("https://docs.python.org/3/whatsnew/3.14.html", out)
        self.assertIn("Release highlights", out)

    def test_web_search_no_results(self):
        with mock.patch.object(
            tools_module, "_get_httpx_client", lambda: _FakeHttpx("<html></html>")
        ):
            self.assertEqual(
                self._run("web_search", {"query": "x"}), "Ничего не найдено."
            )

    def test_fetch_url_guards_and_ok(self):
        self.assertEqual(self._run("fetch_url", {}), "Пустой URL.")
        self.assertEqual(
            self._run("fetch_url", {"url": "ftp://x"}),
            "URL должен начинаться с http:// или https://",
        )
        with mock.patch.object(
            tools_module,
            "_get_httpx_client",
            lambda: _FakeHttpx("<html><body>Hi</body></html>"),
        ):
            self.assertEqual(
                self._run("fetch_url", {"url": "https://93.184.216.34/"}), "Hi"
            )

    def test_fetch_url_rejects_bad_port(self):
        out = self._run("fetch_url", {"url": "http://93.184.216.34:99999/"})
        self.assertIn("Некорректный порт", out)

    def test_fetch_url_blocks_private_targets(self):
        for url in (
            "http://127.0.0.1:8008/v1/models",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://[::1]/",
        ):
            with self.subTest(url=url):
                out = self._run("fetch_url", {"url": url})
                self.assertIn("Адрес заблокирован", out)

    def test_fetch_url_follows_redirects_safely(self):
        class _RedirectClient:
            def __init__(self):
                self.urls = []
                self._last = ""

            def _resp(self):
                self.urls.append(self._last)
                if len(self.urls) == 1:
                    return _FakeResp("", 302, location="http://127.0.0.1:8008/v1")
                return _FakeResp("secret", 200)

            async def get(self, url, **kwargs):
                self._last = url
                return self._resp()

            def stream(self, _method, url, **kwargs):
                self._last = url
                return _FakeStream(self._resp())

        client = _RedirectClient()
        with mock.patch.object(tools_module, "_get_httpx_client", lambda: client):
            out = self._run("fetch_url", {"url": "https://93.184.216.34/"})
        self.assertIn("Адрес заблокирован", out)
        self.assertEqual(client.urls, ["https://93.184.216.34/"])

    def test_run_subagent_guards(self):
        self.assertEqual(
            self._owner_run("run_subagent", {"task": "x"}),
            "Субагенты недоступны.",
        )

    def test_execute_tool_reports_handler_crash(self):
        async def boom(arguments, chat_id, client, stats, unrestricted=False):
            raise RuntimeError("tool down")

        with mock.patch.dict(tools_module._HANDLERS, {"evaluate": boom}):
            out = self._run("evaluate", {"expression": "1+1"})
        self.assertIn("Ошибка инструмента evaluate", out)
        self.assertIn("tool down", out)

    def test_execute_tool_captures_network_error(self):
        import httpx

        async def boom(arguments, chat_id, client, stats, unrestricted=False):
            raise httpx.HTTPError("connection reset")

        with mock.patch.dict(tools_module._HANDLERS, {"fetch_url": boom}):
            out = self._run("fetch_url", {"url": "https://93.184.216.34/"})
        self.assertIn("Ошибка инструмента fetch_url", out)
        self.assertIn("connection reset", out)

    def test_execute_tool_enforces_allowed_set(self):
        allowed = tools_module.tool_names_of(tools_module.BOT_TOOLS)
        self.assertNotIn("read_file", allowed)
        out = asyncio.run(
            tools_module.execute_tool(
                "read_file", {"path": "x"}, -100, None, None, False, allowed
            )
        )
        self.assertIn("недоступен в этой сессии", out)
        out = asyncio.run(
            tools_module.execute_tool("get_time", {}, -100, None, None, False, allowed)
        )
        self.assertIn("utc", out)

    def test_tool_names_of_ignores_broken_schema(self):
        self.assertEqual(tools_module.tool_names_of([{"nope": 1}, "x"]), set())
        self.assertEqual(
            tools_module.tool_names_of(
                [{"function": "x"}, {"function": {}}, {"function": {"name": 1}}, None]
            ),
            set(),
        )
        self.assertEqual(
            tools_module.tool_names_of([{"function": {"name": "get_time"}}]),
            {"get_time"},
        )

    def test_memory_and_skill_tools_work_through_threads(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_tools_store_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for mod in (memory, skills):
            for attr, value in (
                ("DATA_DIR", tmp),
                ("DB_PATH", tmp / f"{mod.__name__}.db"),
            ):
                patcher = mock.patch.object(mod, attr, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            self.addCleanup(mod._initialized.clear)
            mod._initialized.clear()
        created = self._owner_run(
            "memory_remember", {"key": "k1", "value": "v1", "tags": ["a"]}
        )
        self.assertIn("created", created)
        recalled = self._run("memory_recall", {"key": "k1"})
        self.assertIn("v1", recalled)
        listed = self._run("memory_list", {})
        self.assertIn("k1", listed)
        self.assertIn("stats", listed)
        self.assertIn("deleted", self._owner_run("memory_forget", {"key": "k1"}))
        self.assertIn(
            "created", self._owner_run("save_skill", {"name": "s", "body": "b"})
        )
        self.assertIn("s", self._run("load_skill", {"name": "s"}))
        self.assertIn("s", self._run("list_skills", {}))
        self.assertIn("deleted", self._owner_run("delete_skill", {"name": "s"}))

    def test_execute_tool_unknown_name(self):
        self.assertEqual(self._run("nope", {}), "Неизвестная функция: nope")

    def test_run_shell_defaults_to_coder_root(self):
        out = self._owner_run("run_shell", {"command": "cd"})
        self.assertIn("rc=0", out)
        self.assertIn(str(tools_module.CODER_ROOT), out)

    def test_run_shell_cwd_outside_root(self):
        out = self._owner_run("run_shell", {"command": "cd", "cwd": "/etc"})
        self.assertIn("вне разрешённого корня", out)

    def test_search_files_rejects_long_pattern(self):
        out = self._owner_run("search_files", {"pattern": "a" * 500})
        self.assertIn("Слишком длинный pattern", out)

    def test_output_over_cap_is_drained_and_marked(self):
        with mock.patch.object(tools_module, "MAX_PROCESS_BYTES", 64):
            out = self._owner_run(
                "run_shell",
                {"command": f'"{sys.executable}" -c "print(chr(65)*4096)"'},
            )
        self.assertIn("rc=0", out)
        self.assertIn("A" * 60, out)
        self.assertIn("Вывод обрезан.", out)

    def test_output_over_cap_keeps_exit_code(self):
        with mock.patch.object(tools_module, "MAX_PROCESS_BYTES", 64):
            out = self._owner_run(
                "execute_script",
                {"code": "print('B' * 4096)\nraise SystemExit(3)"},
            )
        self.assertIn("Вывод обрезан.", out)
        self.assertTrue(out.startswith("rc=3") or "\nrc=3" in out)

    def test_timeout_returns_partial_output(self):
        out = self._owner_run(
            "run_shell",
            {
                "command": (
                    f'"{sys.executable}" -u -c "print(chr(67)*200);import time;'
                    'time.sleep(30)"'
                ),
                "timeout": 1,
            },
        )
        self.assertIn("C" * 100, out)
        self.assertIn("Таймаут 1s", out)

    def test_web_search_reports_http_status_label(self):
        class _RateLimited:
            def stream(self, _method, _url, **_kwargs):
                request = httpx.Request("GET", "https://search.test/")
                response = httpx.Response(429, request=request)
                raise httpx.HTTPStatusError("rate", request=request, response=response)

        with mock.patch.object(
            tools_module, "_get_httpx_client", lambda: _RateLimited()
        ):
            out = self._run("web_search", {"query": "x"})
        self.assertIn("Ошибка поиска", out)
        self.assertIn("HTTP 429", out)

    def test_web_search_reports_timeout_label(self):
        class _Slow:
            def stream(self, _method, _url, **_kwargs):
                raise httpx.ConnectTimeout("slow")

        with mock.patch.object(tools_module, "_get_httpx_client", lambda: _Slow()):
            out = self._run("web_search", {"query": "x"})
        self.assertIn("Ошибка поиска", out)
        self.assertIn("ConnectTimeout", out)

    def test_web_search_applies_result_limit(self):
        html = (
            '<a class="result__a" href="https://one.test/">One</a>'
            '<a class="result__a" href="https://two.test/">Two</a>'
        )
        with mock.patch.object(
            tools_module, "_get_httpx_client", lambda: _FakeHttpx(html)
        ):
            out = self._run("web_search", {"query": "x", "limit": 1})
        self.assertIn("One", out)
        self.assertNotIn("Two", out)

    def test_web_search_clamps_limit_range(self):
        html = '<a class="result__a" href="https://one.test/">One</a>'
        with mock.patch.object(
            tools_module, "_get_httpx_client", lambda: _FakeHttpx(html)
        ):
            self.assertIn("One", self._run("web_search", {"query": "x", "limit": 99}))
            self.assertIn(
                "One", self._run("web_search", {"query": "x", "limit": "junk"})
            )

    def test_fetch_url_reports_download_error(self):
        class _Broken:
            def stream(self, _method, _url, **_kwargs):
                raise httpx.ConnectError("down")

        with mock.patch.object(tools_module, "_get_httpx_client", lambda: _Broken()):
            out = self._run("fetch_url", {"url": "https://93.184.216.34/"})
        self.assertIn("Ошибка загрузки", out)
        self.assertIn("down", out)

    def test_fetch_url_stops_after_redirect_budget(self):
        class _Looping:
            def stream(self, _method, _url, **_kwargs):
                return _FakeStream(
                    _FakeResp("", 302, location="https://93.184.216.34/next")
                )

        async def allowed(_url):
            return ""

        with (
            mock.patch.object(tools_module, "_get_httpx_client", lambda: _Looping()),
            mock.patch.object(tools_module, "_check_public_url", allowed),
        ):
            out = self._run("fetch_url", {"url": "https://93.184.216.34/"})
        self.assertIn("Слишком много перенаправлений", out)

    def test_fetch_url_reports_total_timeout(self):
        class _Scope:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                raise TimeoutError

        class _AsyncioProxy:
            def __init__(self, timeout):
                self.timeout = timeout

            def __getattr__(self, name):
                return getattr(asyncio, name)

        delays = []

        def fake_timeout(delay):
            delays.append(delay)
            return _Scope()

        proxy = _AsyncioProxy(fake_timeout)
        with mock.patch.object(tools_module, "asyncio", proxy):
            out = self._run("fetch_url", {"url": "https://93.184.216.34/"})
        self.assertEqual(delays, [tools_module.FETCH_TIMEOUT + 5])
        self.assertIn("Таймаут", out)

    def test_get_entity_tools_report_telethon_errors(self):
        class _Failing:
            async def get_entity(self, _key):
                raise RPCError(request=None, message="chat not found")

            async def get_me(self):
                raise RPCError(request=None, message="account not found")

        client = _Failing()
        self.assertIn(
            "Ошибка получения чата",
            asyncio.run(tools_module.execute_tool("get_chat_info", {}, 1, client)),
        )
        self.assertIn(
            "Ошибка получения пользователя",
            asyncio.run(
                tools_module.execute_tool("get_user_info", {"handle": "u"}, 1, client)
            ),
        )
        self.assertIn(
            "Ошибка получения профиля",
            asyncio.run(tools_module.execute_tool("get_profile", {}, 1, client)),
        )

    def test_run_shell_reports_launch_failure(self):
        async def broken(*_args, **_kwargs):
            raise OSError("нет оболочки")

        saved = asyncio.create_subprocess_shell
        self.addCleanup(setattr, asyncio, "create_subprocess_shell", saved)
        asyncio.create_subprocess_shell = broken
        out = _owner_tool("run_shell", {"command": "echo x"}, 1)
        self.assertIn("Ошибка запуска", out)
        self.assertIn("нет оболочки", out)

    def test_run_shell_rejects_missing_workdir(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_wd_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "a.txt").write_text("x", encoding="utf-8")
        saved_root = tools_module.CODER_ROOT
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved_root)
        tools_module.CODER_ROOT = tmp
        out = _owner_tool("run_shell", {"command": "cd", "cwd": "a.txt"}, 1)
        self.assertIn("Каталог не найден", out)

    def test_execute_script_rejects_oversized_code(self):
        saved = tools_module.MAX_SCRIPT_BYTES
        self.addCleanup(setattr, tools_module, "MAX_SCRIPT_BYTES", saved)
        tools_module.MAX_SCRIPT_BYTES = 4
        out = _owner_tool("execute_script", {"code": "print(1)"}, 1)
        self.assertIn("Слишком большой объём", out)

    def test_run_subagent_requires_task(self):
        for attr, value in (
            ("run_subagents", None),
            ("is_configured", lambda: True),
        ):
            saved = getattr(subagents, attr)
            self.addCleanup(setattr, subagents, attr, saved)
            setattr(subagents, attr, value)
        out = _owner_tool("run_subagent", {}, 1)
        self.assertEqual(out, "Нужна задача: task или tasks.")

    def test_run_subagent_rejects_non_list_tasks(self):
        seen = {}

        async def fake_run(tasks, **_kwargs):
            seen["tasks"] = tasks
            return []

        for attr, value in (
            ("run_subagents", fake_run),
            ("is_configured", lambda: True),
        ):
            saved = getattr(subagents, attr)
            self.addCleanup(setattr, subagents, attr, saved)
            setattr(subagents, attr, value)
        self.assertIn(
            "списком строк",
            _owner_tool("run_subagent", {"tasks": {"a": 1}}, 1),
        )
        self.assertEqual(seen, {})
        self.assertEqual(_owner_tool("run_subagent", {"tasks": "одна задача"}, 1), "[]")
        self.assertEqual(seen["tasks"], ["одна задача"])
        self.assertEqual(
            _owner_tool("run_subagent", {"tasks": ["  ", "первая", ""]}, 1), "[]"
        )
        self.assertEqual(seen["tasks"], ["первая"])

    def test_run_subagent_passes_numeric_options(self):
        seen = {}

        async def fake_run(tasks, **kwargs):
            seen.update(kwargs)
            seen["tasks"] = tasks
            return []

        for attr, value in (
            ("run_subagents", fake_run),
            ("is_configured", lambda: True),
        ):
            saved = getattr(subagents, attr)
            self.addCleanup(setattr, subagents, attr, saved)
            setattr(subagents, attr, value)
        _owner_tool(
            "run_subagent", {"tasks": ["a", "b"], "concurrency": 3, "tools": 5}, 1
        )
        self.assertEqual(seen["tasks"], ["a", "b"])
        self.assertEqual(seen["concurrency"], 3)
        self.assertIsNone(seen["tool_names"])
        self.assertIsNone(seen["max_rounds"])
        self.assertFalse(seen["verify"])
        with mock.patch.object(tools_module, "OWNER_ONLY_TOOLS", frozenset()):
            asyncio.run(tools_module.execute_tool("run_subagent", {"task": "a"}, 1))
        self.assertTrue(seen["verify"])


class ToolsInternalsTest(BotTestCase):
    RESULT_CASES = (("5-2", "3"), ("5/2", "2.5"))
    ERROR_CASES = (
        ("1 << 2", "Недопустимый оператор"),
        ("~5", "Недопустимый унарный оператор"),
        ("1 < 2", "Недопустимая конструкция"),
        ("(1).real", "Недопустимая конструкция"),
        ("pow(10**41, 1000)", "Слишком большое число"),
        ("max(" + "1," * 6000 + "1)", "Слишком сложное выражение"),
    )

    def test_safe_eval_arithmetic_paths(self):
        for expression, expected in self.RESULT_CASES:
            with self.subTest(expression=expression):
                self.assertEqual(tools_module.safe_eval(expression), expected)

    def test_safe_eval_rejects_unsupported_constructs(self):
        for expression, expected in self.ERROR_CASES:
            with self.subTest(expression=expression[:24]):
                self.assertIn(expected, tools_module.safe_eval(expression))

    def test_int_arg_clamps_and_falls_back(self):
        for arguments, expected in (
            ({"t": "junk"}, 30),
            ({"t": None}, 30),
            ({}, 30),
            ({"t": "7"}, 7),
            ({"t": 5000}, 300),
            ({"t": -5}, 1),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    tools_module._int_arg(arguments, "t", 30, 1, 300), expected
                )

    def test_opt_int_arg_keeps_absent_value(self):
        self.assertIsNone(tools_module._opt_int_arg({}, "concurrency", 1, 16))
        self.assertIsNone(
            tools_module._opt_int_arg({"concurrency": None}, "concurrency", 1, 16)
        )
        self.assertEqual(
            tools_module._opt_int_arg({"concurrency": "3"}, "concurrency", 1, 16), 3
        )
        self.assertEqual(
            tools_module._opt_int_arg({"concurrency": 99}, "concurrency", 1, 16), 16
        )

    def test_int_arg_survives_infinite_float(self):
        self.assertEqual(tools_module._int_arg({"t": math.inf}, "t", 30, 1, 300), 30)
        self.assertEqual(tools_module._int_arg({"t": -math.inf}, "t", 30, 1, 300), 30)

    def test_report_item_bounds_tools_used(self):
        item = {
            "name": "n",
            "task": "t",
            "ok": True,
            "rounds": 1,
            "tools_used": ["tool"] * 500,
            "result": "r",
        }
        out = tools_module._subagent_report([item])
        self.assertLessEqual(len(out), tools_module.SUBAGENT_REPORT_CHARS)
        self.assertLessEqual(len(json.loads(out)[0]["tools_used"]), 60)
        single = json.loads(
            tools_module._subagent_report([dict(item, tools_used=["x" * 4000] * 50)])
        )
        self.assertLessEqual(len(single[0]["tools_used"]), 0)

    def test_subagent_report_drops_items_when_needed(self):
        results = [
            {
                "name": "n" * tools_module.SUBAGENT_NAME_CHARS,
                "task": "t" * tools_module.SUBAGENT_TASK_CHARS,
                "ok": True,
                "rounds": 1,
                "tools_used": ["x"] * 60,
                "result": "",
            }
            for _ in range(40)
        ]
        out = tools_module._subagent_report(results)
        self.assertLessEqual(len(out), tools_module.SUBAGENT_REPORT_CHARS)
        parsed = json.loads(out)
        self.assertLess(len(parsed), 40)
        self.assertTrue(parsed)
        one = tools_module._subagent_report(
            [dict(results[0], tools_used=["y" * 200] * 300)]
        )
        self.assertLessEqual(len(one), tools_module.SUBAGENT_REPORT_CHARS)
        self.assertLess(len(json.loads(one)[0]["tools_used"]), 60)

    def test_too_big_ignores_unreadable_path(self):
        missing = Path(tempfile.gettempdir()) / "нет.такого.py"
        self.assertEqual(tools_module._too_big(missing), "")

    def test_resolve_path_reports_unresolvable_target(self):
        real = Path.resolve
        seen = []

        def flaky(self, *args, **kwargs):
            seen.append(self.name)
            if len(seen) > 1:
                raise OSError("битая ссылка")
            return real(self, *args, **kwargs)

        with mock.patch.object(Path, "resolve", flaky):
            path, err = tools_module._resolve_path("куда-то")
        self.assertIsNone(path)
        self.assertIn("Не удалось разрешить путь", err)

    def test_render_response_hard_truncates_huge_tool_list(self):
        names = [f"tool_{index}" for index in range(1000)]
        text = tools_module.render_response("п" * 800, ["р" * 3000], names, "")
        self.assertEqual(len(text), tools_module.MAX_RENDER_CHARS)
        self.assertTrue(text.endswith("…"))

    def test_output_sink_ignores_chunks_after_truncation(self):
        sink = tools_module._OutputSink(4)
        sink.feed(b"abcde")
        sink.feed(b"fgh")
        self.assertEqual(sink.size, 4)
        self.assertEqual(sink.text(), "abcd")

    def test_spawn_kwargs_depends_on_platform(self):
        with mock.patch.object(tools_module, "os", SimpleNamespace(name="nt")):
            self.assertEqual(tools_module._spawn_kwargs(), {})
        with mock.patch.object(tools_module, "os", SimpleNamespace(name="posix")):
            self.assertEqual(tools_module._spawn_kwargs(), {"start_new_session": True})

    def test_kill_process_now_skips_finished_process(self):
        killed = []
        proc = SimpleNamespace(returncode=0, pid=11, kill=lambda: killed.append(1))
        tools_module._kill_process_now(proc)
        self.assertEqual(killed, [])

    def test_kill_process_now_uses_group_kill_on_posix(self):
        signals = []
        killed = []
        proc = SimpleNamespace(
            returncode=None,
            pid=4242,
            kill=lambda: killed.append("proc"),
        )
        fake_os = SimpleNamespace(
            name="posix", kill=lambda pid, sig: signals.append((pid, sig))
        )
        with mock.patch.object(tools_module, "os", fake_os):
            tools_module._kill_process_now(proc)
        self.assertEqual(signals, [(-4242, getattr(signal, "SIGKILL", 9))])
        self.assertEqual(killed, ["proc"])

    def test_close_pipes_handles_missing_transport(self):
        tools_module._close_pipes(SimpleNamespace())
        closed = []
        proc = SimpleNamespace(
            _transport=SimpleNamespace(close=lambda: closed.append(1))
        )
        tools_module._close_pipes(proc)
        self.assertEqual(closed, [1])

    def test_close_pipes_swallows_transport_failure(self):
        def boom():
            raise RuntimeError("уже закрыт")

        tools_module._close_pipes(
            SimpleNamespace(_transport=SimpleNamespace(close=boom))
        )

    def test_collect_process_reports_hanging_process(self):
        class _Reader:
            def __init__(self, chunks):
                self.chunks = list(chunks)

            async def read(self, _size):
                return self.chunks.pop(0) if self.chunks else b""

        class _Proc:
            def __init__(self):
                self.pid = 424242
                self.returncode = None
                self.stdout = _Reader(["часть".encode()])
                self.stderr = _Reader([])
                self.killed = False

            async def wait(self):
                return None

            def kill(self):
                self.killed = True

        proc = _Proc()
        fake_os = SimpleNamespace(name="posix", kill=lambda *_args: None)
        with mock.patch.object(tools_module, "os", fake_os):
            rc, out, err, note = asyncio.run(
                tools_module._collect_process(proc, 5, tools_module.MAX_PROCESS_BYTES)
            )
        self.assertEqual(rc, -1)
        self.assertEqual(out, "часть")
        self.assertEqual(err, "")
        self.assertIn("Процесс не завершился", note)
        self.assertTrue(proc.killed)

    def test_exit_code_falls_back_to_minus_one(self):
        self.assertEqual(tools_module._exit_code(SimpleNamespace(returncode=None)), -1)
        self.assertEqual(tools_module._exit_code(SimpleNamespace(returncode=3)), 3)

    def test_format_process_result_appends_stderr(self):
        self.assertEqual(
            tools_module._format_process_result(0, "вывод", ""), "rc=0\nstdout:\nвывод"
        )
        self.assertIn(
            "stderr:\nошибка", tools_module._format_process_result(1, "", "ошибка")
        )

    def test_read_capped_marks_truncation_on_exact_cap(self):
        class _Resp:
            async def aiter_bytes(self):
                for chunk in (b"ab", b"cd", b"ef"):
                    yield chunk

        body, cut = asyncio.run(tools_module._read_capped(_Resp(), 4))
        self.assertEqual(body, b"abcd")
        self.assertTrue(cut)

    def test_is_public_addr_rejects_garbage(self):
        self.assertFalse(tools_module._is_public_addr("не адрес"))
        self.assertTrue(tools_module._is_public_addr("8.8.8.8"))
        self.assertFalse(tools_module._is_public_addr("192.168.0.1"))

    def test_is_public_addr_blocks_internal_reachable_forms(self):
        for addr in (
            "64:ff9b::7f00:1",
            "::a00:1",
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:0:0",
            "2002:7f00:1::",
            "ff02::1",
            "224.0.0.1",
            "127.0.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "::1",
            "::",
            "fc00::1",
            "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
        ):
            with self.subTest(addr=addr):
                self.assertFalse(tools_module._is_public_addr(addr))
        for addr in (
            "8.8.8.8",
            "2001:4860:4860::8888",
            "1.1.1.1",
            "::ffff:8.8.8.8",
        ):
            with self.subTest(addr=addr):
                self.assertTrue(tools_module._is_public_addr(addr))

    def test_clip_process_result_marks_truncation(self):
        text = tools_module._format_process_result(0, "a" * 5000, "ошибка")
        clipped = tools_module._clip_process_result(text, 1000)
        self.assertLessEqual(len(clipped), 1100)
        self.assertIn("вывод обрезан", clipped)
        short = tools_module._format_process_result(0, "ok", "")
        self.assertIs(tools_module._clip_process_result(short, 1000), short)

    def test_read_file_output_is_bounded(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_readout_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        saved_root = tools_module.CODER_ROOT
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved_root)
        tools_module.CODER_ROOT = tmp
        (tmp / "huge.txt").write_text("строка данных\n" * 20000, encoding="utf-8")
        out = _owner_tool("read_file", {"path": "huge.txt", "limit": 5000}, 1)
        self.assertLessEqual(len(out), tools_module.MAX_READ_OUTPUT + 500)
        self.assertIn("…", out)

    def test_check_public_url_reports_missing_host(self):
        out = asyncio.run(tools_module._check_public_url("http:///страница"))
        self.assertIn("URL без хоста", out)

    def test_check_public_url_reports_dns_failure(self):
        async def failing(_host, _port):
            raise OSError("dns недоступен")

        with mock.patch.object(tools_module, "_resolve_addrs", failing):
            out = asyncio.run(tools_module._check_public_url("https://host.test/"))
        self.assertIn("Не удалось разрешить хост host.test", out)

    def test_check_public_url_reports_empty_dns_answer(self):
        async def empty(_host, _port):
            return []

        with mock.patch.object(tools_module, "_resolve_addrs", empty):
            out = asyncio.run(tools_module._check_public_url("https://host.test/"))
        self.assertIn("Не удалось разрешить хост host.test.", out)

    def test_check_public_url_rejects_blocked_scheme(self):
        out = asyncio.run(tools_module._check_public_url("file:///etc/passwd"))
        self.assertIn("Схема заблокирована", out)

    def test_check_public_url_blocks_internal_targets(self):
        for host, addr in (
            ("metadata.test", "169.254.169.254"),
            ("nat64.test", "64:ff9b::7f00:1"),
            ("compat.test", "::a00:1"),
            ("mapped.test", "::ffff:127.0.0.1"),
        ):
            with self.subTest(host=host):

                async def fake_resolve(_host, _port, _addr=addr):
                    return [_addr]

                with mock.patch.object(tools_module, "_resolve_addrs", fake_resolve):
                    out = asyncio.run(
                        tools_module._check_public_url(f"https://{host}/")
                    )
                self.assertIn("Адрес заблокирован", out)

    def test_fetch_url_reports_error_instead_of_raising(self):
        for url in (
            "http://example.test/\x00",
            "http://exa mple.test/",
            "http://[::1/",
        ):
            with self.subTest(url=url):
                out = _owner_tool("fetch_url", {"url": url}, 1)
                self.assertIsInstance(out, str)
                self.assertNotIn("Traceback", out)

    def test_error_label_prefers_http_status(self):
        request = httpx.Request("GET", "https://search.test/")
        response = httpx.Response(429, request=request)
        exc = httpx.HTTPStatusError("rate", request=request, response=response)
        self.assertEqual(tools_module._error_label(exc), "HTTP 429")
        self.assertEqual(tools_module._error_label(OSError("down")), "OSError")

    def test_strip_tags_unescapes_entities(self):
        self.assertEqual(
            tools_module._strip_tags("<b>жирный &amp; текст</b>"), "жирный & текст"
        )

    def test_parse_brave_skips_blocks_without_link(self):
        raw = '<div class="snippet svelte-a" data-type="web"><span>нет ссылки</span></div>'
        self.assertEqual(tools_module._parse_brave(raw, 5), [])

    def test_parse_brave_honours_limit(self):
        raw = (
            '<div class="snippet a" data-type="web"><a href="https://one.test/">'
            '<div class="title t">Раз</div></a></div>'
            '<div class="snippet b" data-type="web"><a href="https://two.test/">'
            '<div class="title t">Два</div></a></div>'
        )
        self.assertEqual(tools_module._parse_brave(raw, 1), ["Раз\nhttps://one.test/"])
        self.assertEqual(len(tools_module._parse_brave(raw, 5)), 2)

    def test_parse_brave_skips_blocks_without_title(self):
        raw = (
            '<div class="snippet a" data-type="web">'
            '<a href="https://one.test/">без заголовка</a></div>'
        )
        self.assertEqual(tools_module._parse_brave(raw, 5), ["https://one.test/"])

    def test_parse_ddg_unescapes_redirect_and_limits(self):
        raw = (
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fex.test%2Fa">'
            "Первый &amp; итог</a>"
            '<a class="result__a" href="https://two.test/b">Второй</a>'
        )
        first = "Первый & итог\nhttps://ex.test/a"
        self.assertEqual(
            tools_module._parse_ddg(raw, 5), [first, "Второй\nhttps://two.test/b"]
        )
        self.assertEqual(tools_module._parse_ddg(raw, 1), [first])

    def test_parse_ddg_keeps_plain_href(self):
        raw = '<a class="result__a" href="https://plain.test/x">Ровный</a>'
        self.assertEqual(
            tools_module._parse_ddg(raw, 5), ["Ровный\nhttps://plain.test/x"]
        )

    def test_tool_name_list_filters_bad_types(self):
        self.assertIsNone(tools_module._tool_name_list(None))
        self.assertIsNone(tools_module._tool_name_list(5))
        self.assertIsNone(tools_module._tool_name_list(["  ", ""]))
        self.assertEqual(tools_module._tool_name_list(("a", " b ")), ["a", "b"])
        self.assertEqual(tools_module._tool_name_list("get_time"), ["get_time"])

    def test_subagent_report_fills_defaults(self):
        report = json.loads(tools_module._subagent_report([{}]))
        self.assertEqual(report[0]["name"], "universal")
        self.assertEqual(report[0]["task"], "")
        self.assertFalse(report[0]["ok"])
        self.assertEqual(report[0]["rounds"], 0)
        self.assertEqual(report[0]["tools_used"], [])
        self.assertEqual(report[0]["result"], "")

    def test_is_safe_glob_rejects_escapes(self):
        for pattern in ("/etc/*", "\\windows\\*", "~/x", "C:/x", "../x", "a/../../b"):
            with self.subTest(glob=pattern):
                self.assertFalse(tools_module._is_safe_glob(pattern))
        self.assertTrue(tools_module._is_safe_glob("*.py"))
        self.assertTrue(tools_module._is_safe_glob("sub/*.py"))

    def test_httpx_singleton_lifecycle_across_loops(self):
        saved = tools_module._httpx_singleton
        saved_loop = tools_module._httpx_loop
        self.addCleanup(setattr, tools_module, "_httpx_singleton", saved)
        self.addCleanup(setattr, tools_module, "_httpx_loop", saved_loop)
        tools_module._httpx_singleton = None
        tools_module._httpx_loop = None

        outside = tools_module._get_httpx_client()
        self.assertIsNone(tools_module._httpx_loop)
        self.addCleanup(asyncio.run, outside.aclose())

        async def scenario():
            first = tools_module._get_httpx_client()
            second = tools_module._get_httpx_client()
            self.assertIs(first, second)
            await tools_module.close_httpx_client()
            return first

        inside = asyncio.run(scenario())
        self.assertIsNot(inside, outside)
        self.assertIsNone(tools_module._httpx_singleton)
        asyncio.run(inside.aclose())
        asyncio.run(tools_module.close_httpx_client())

    def test_httpx_stale_client_is_closed_on_old_loop(self):
        class _Task:
            def __init__(self):
                self.callbacks = []

            def add_done_callback(self, callback):
                self.callbacks.append(callback)

        class _OldLoop:
            def __init__(self):
                self.tasks = []

            def is_running(self):
                return True

            def create_task(self, coro):
                coro.close()
                task = _Task()
                self.tasks.append(task)
                return task

        class _FakeClient:
            is_closed = False

            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        saved = tools_module._httpx_singleton
        saved_loop = tools_module._httpx_loop
        self.addCleanup(setattr, tools_module, "_httpx_singleton", saved)
        self.addCleanup(setattr, tools_module, "_httpx_loop", saved_loop)
        stale = _FakeClient()
        old_loop = _OldLoop()
        tools_module._httpx_singleton = stale
        tools_module._httpx_loop = old_loop

        fresh = _FakeClient()

        async def scenario():
            return tools_module._get_httpx_client()

        with mock.patch.object(tools_module, "_build_httpx_client", lambda: fresh):
            result = asyncio.run(scenario())
        self.assertIs(result, fresh)
        self.assertFalse(stale.closed)
        self.assertEqual(len(old_loop.tasks), 1)
        pending = old_loop.tasks[0]
        self.addCleanup(tools_module._httpx_closing.discard, pending)
        self.assertIn(pending, tools_module._httpx_closing)
        pending.callbacks[0](pending)
        self.assertNotIn(pending, tools_module._httpx_closing)

    def test_scan_files_stops_when_event_is_set(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_scanstop_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "a.txt").write_text("игла", encoding="utf-8")
        stop = threading.Event()
        stop.set()
        matches, note = tools_module._scan_files(tmp, "*", re.compile("игла"), 10, stop)
        self.assertEqual(matches, [])
        self.assertEqual(note, "остановлено по таймауту")

    def test_scan_files_stops_on_file_budget(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_scanbudget_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ("a.txt", "b.txt"):
            (tmp / name).write_text("игла", encoding="utf-8")
        saved = tools_module.MAX_SEARCH_FILES
        self.addCleanup(setattr, tools_module, "MAX_SEARCH_FILES", saved)
        tools_module.MAX_SEARCH_FILES = 1
        matches, note = tools_module._scan_files(
            tmp, "*", re.compile("игла"), 10, threading.Event()
        )
        self.assertEqual(len(matches), 1)
        self.assertIn("просмотрено не больше 1 файлов", note)

    def test_scan_files_skips_unresolvable_entries(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_scanres_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ("a.txt", "b.txt"):
            (tmp / name).write_text("игла", encoding="utf-8")
        real = Path.resolve

        def flaky(self, *args, **kwargs):
            if self.name == "b.txt":
                raise OSError("битая ссылка")
            return real(self, *args, **kwargs)

        with mock.patch.object(Path, "resolve", flaky):
            matches, note = tools_module._scan_files(
                tmp, "*", re.compile("игла"), 10, threading.Event()
            )
        self.assertEqual(len(matches), 1)
        self.assertIn("a.txt", matches[0])
        self.assertEqual(note, "")

    def test_scan_files_skips_unreadable_entries(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_scanread_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "a.txt").write_text("игла", encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=ValueError("мусор")):
            matches, note = tools_module._scan_files(
                tmp, "*", re.compile("игла"), 10, threading.Event()
            )
        self.assertEqual(matches, [])
        self.assertEqual(note, "")

    def test_scan_files_stops_inside_long_file(self):
        class _CountingStop:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 1

        tmp = Path(tempfile.mkdtemp(prefix="danybot_scanline_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "long.txt").write_text("строка\n" * 400, encoding="utf-8")
        stop = _CountingStop()
        matches, note = tools_module._scan_files(
            tmp, "*", re.compile("иной текст"), 10000, stop
        )
        self.assertEqual(matches, [])
        self.assertEqual(note, "остановлено по таймауту")
        self.assertEqual(stop.calls, 2)

    def test_scan_files_searches_single_file_root(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_scanone_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "a.txt"
        target.write_text("игла\nигла", encoding="utf-8")
        matches, note = tools_module._scan_files(
            target, "*", re.compile("игла"), 10, threading.Event()
        )
        self.assertEqual(len(matches), 2)
        self.assertEqual(note, "")

    def test_list_entries_sorts_files_after_directories(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_entries_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "z.txt").write_text("x", encoding="utf-8")
        (tmp / "a_dir").mkdir()
        (tmp / "b.txt").write_text("x", encoding="utf-8")
        entries, total = tools_module._list_entries(tmp)
        self.assertEqual(total, 3)
        self.assertEqual([entry.name for entry in entries], ["a_dir", "b.txt", "z.txt"])

    def test_list_entries_handles_missing_directory(self):
        with self.assertRaises(OSError):
            tools_module._list_entries(Path(tempfile.gettempdir()) / "нет.каталога")


class CoreHelpersTest(BotTestCase):
    def test_async_saver_writes_and_flushes(self):
        calls = []
        saver = core.AsyncSaver(lambda: calls.append(1), delay=0.01)

        async def run():
            saver.mark_dirty()
            await asyncio.sleep(0.05)
            await saver.flush()

        asyncio.run(run())
        self.assertGreaterEqual(len(calls), 1)

    def test_async_saver_keeps_dirty_after_failed_write(self):
        calls = []

        def writer():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("no space")

        saver = core.AsyncSaver(writer, delay=0.01)
        dirty = []

        async def run():
            saver.mark_dirty()
            await asyncio.sleep(0.05)
            dirty.append(saver._dirty)
            await saver.flush()

        asyncio.run(run())
        self.assertEqual(dirty, [True])
        self.assertEqual(len(calls), 2)

    def test_state_file_write_is_atomic(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_atomic_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "state.json"
        self.assertTrue(
            core.save_state_file(
                target,
                {
                    "model_overrides": {1: "m"},
                    "coder_chats": {1},
                    "reasoning_hidden": set(),
                    "tools_hidden": set(),
                },
            )
        )
        self.assertEqual([p.name for p in tmp.iterdir()], ["state.json"])
        parsed = core.load_state_file(target)
        if parsed is None:
            self.fail("state file did not parse")
        self.assertEqual(parsed["model_overrides"], {1: "m"})

    def test_state_file_write_failure_reports_false(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_atomic_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "state.json"
        target.mkdir()
        self.assertFalse(
            core.save_state_file(
                target,
                {
                    "model_overrides": {},
                    "coder_chats": set(),
                    "reasoning_hidden": set(),
                    "tools_hidden": set(),
                },
            )
        )
        self.assertEqual([p.name for p in tmp.iterdir()], ["state.json"])

    def test_stream_answer_recovers_from_unexpected_error(self):
        store = _StoreStub()
        edited = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            edited.append(text)
            return True

        async def reply_fn(event, text):
            return SimpleNamespace(id=555)

        def render(prefix, reasoning, tools, answer):
            return answer

        async def stream_fn(*_args, **_kwargs):
            raise KeyError("broken tool")

        with self.assertRaises(KeyError):
            asyncio.run(
                core.stream_answer(
                    store,
                    event=None,
                    chat_id=1,
                    is_self=False,
                    messages=[],
                    model="m",
                    prefix="",
                    self_edit_id=None,
                    render_fn=render,
                    edit_fn=edit_fn,
                    reply_fn=reply_fn,
                    action=_NullAsyncContext(),
                    stream_fn=stream_fn,
                )
            )
        self.assertTrue(edited)
        self.assertIn("Ошибка", edited[-1])
        self.assertNotIn((1, 555), store.recent_reply_ids)

    def test_stream_answer_reports_error_once_without_output(self):
        store = _StoreStub()
        edited = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            edited.append(text)
            return True

        async def reply_fn(event, text):
            return SimpleNamespace(id=556)

        def render(prefix, reasoning, tools, answer):
            return answer

        async def stream_fn(*_args, **_kwargs):
            raise KeyError("broken tool")

        with self.assertRaises(KeyError):
            asyncio.run(
                core.stream_answer(
                    store,
                    event=None,
                    chat_id=1,
                    is_self=False,
                    messages=[],
                    model="m",
                    prefix="",
                    self_edit_id=None,
                    render_fn=render,
                    edit_fn=edit_fn,
                    reply_fn=reply_fn,
                    action=_NullAsyncContext(),
                    stream_fn=stream_fn,
                )
            )
        self.assertEqual(edited[-1], core.ERROR_NOTICE)

    def test_stream_answer_marks_error_after_partial_answer(self):
        store = _StoreStub()
        edited = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            edited.append(text)
            return True

        async def reply_fn(event, text):
            return SimpleNamespace(id=557)

        def render(prefix, reasoning, tools, answer):
            return answer

        async def stream_fn(_messages, _model, _chat_id, on_delta, *_args, **_kwargs):
            await on_delta("первая часть")
            raise ValueError("поток оборвался")

        with self.assertRaises(ValueError):
            asyncio.run(
                core.stream_answer(
                    store,
                    event=None,
                    chat_id=1,
                    is_self=False,
                    messages=[],
                    model="m",
                    prefix="",
                    self_edit_id=None,
                    render_fn=render,
                    edit_fn=edit_fn,
                    reply_fn=reply_fn,
                    action=_NullAsyncContext(),
                    stream_fn=stream_fn,
                )
            )
        self.assertEqual(edited[-1], f"первая часть\n\n{core.ERROR_NOTICE}")
        self.assertNotIn((1, 557), store.recent_reply_ids)

    def test_final_text_without_error_keeps_answer(self):
        state = {"reasoning_parts": [], "tool_parts": []}
        self.assertEqual(
            core._final_text(state, lambda: "ответ", "ответ", None), "ответ"
        )
        self.assertEqual(
            core._final_text(state, lambda: "…", "", None),
            core.EMPTY_ANSWER,
        )
        state["tool_parts"] = ["get_time"]
        rendered = core._final_text(
            state,
            lambda: tools_module.render_response("", [], ["get_time"], "…"),
            "",
            None,
        )
        self.assertIn("get_time", rendered)

    def test_stream_answer_sends_message_when_placeholder_failed(self):
        store = _StoreStub()
        sent = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            return True

        async def reply_fn(event, text):
            sent.append(text)

        def render(prefix, reasoning, tools, answer):
            return answer

        async def stream_fn(*_args, **_kwargs):
            return "итог"

        answer = asyncio.run(
            core.stream_answer(
                store,
                event=None,
                chat_id=1,
                is_self=False,
                messages=[],
                model="m",
                prefix="",
                self_edit_id=None,
                render_fn=render,
                edit_fn=edit_fn,
                reply_fn=reply_fn,
                action=_NullAsyncContext(),
                stream_fn=stream_fn,
            )
        )
        self.assertEqual(answer, "итог")
        self.assertIn("итог", sent)

    def test_safe_reply_empty_and_success(self):
        event = _FakeReplyEvent()
        recent = set()
        sent = asyncio.run(core.safe_reply(event, "   ", 3, recent))
        self.assertEqual(event.replies, ["…"])
        if sent is None:
            self.fail("safe_reply returned None")
        self.assertEqual(sent.id, 77)
        self.assertIn((5, 77), recent)

    def test_safe_reply_failure_returns_none(self):
        event = _FakeReplyEvent(fail_times=5)
        self.assertIsNone(asyncio.run(core.safe_reply(event, "hi", 2, set())))

    def test_edit_text_success_and_failure(self):
        client = RichFakeClient()

        async def run():
            return await core.edit_text(client, 1, 2, "t", 2)

        self.assertTrue(asyncio.run(run()))

    def test_fetch_replied_text(self):
        class _Msg:
            is_reply = True

            async def get_reply_message(self):
                return SimpleNamespace(message="orig")

        self.assertEqual(asyncio.run(core.fetch_replied_text(_Msg())), "orig")

    def test_fetch_replied_text_none(self):
        class _Msg:
            is_reply = False

        self.assertIsNone(asyncio.run(core.fetch_replied_text(_Msg())))

    def test_check_cooldown(self):
        activity = {}
        self.assertFalse(core.check_cooldown(1, 100.0, 10.0, activity))
        last = activity[1]
        self.assertTrue(core.check_cooldown(1, last + 5.0, 10.0, activity))

    def test_check_cooldown_allows_first_message_on_fresh_clock(self):
        activity = {}
        self.assertFalse(core.check_cooldown(1, 0.5, 60.0, activity))
        self.assertIn(1, activity)

    def test_check_cooldown_disabled(self):
        self.assertFalse(core.check_cooldown(1, 100.0, 0.0, {}))

    def test_handle_command_state_clear(self):
        hist = {1: deque([{"role": "user", "content": "x"}], maxlen=5)}
        resp = core.handle_command_state(
            ("clear", None),
            1,
            True,
            hist,
            {},
            5,
            5,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("Контекст очищен. / Context cleared.", False, True))
        self.assertEqual(len(hist[1]), 0)

    def test_handle_command_state_clear_keeps_deque_identity(self):
        hist = {1: deque([{"role": "user", "content": "x"}], maxlen=5)}
        original = hist[1]
        core.handle_command_state(
            ("clear", None), 1, True, hist, {}, 5, 5, "m", [], "h"
        )
        self.assertIs(hist[1], original)
        original.append({"role": "assistant", "content": "late"})
        self.assertEqual(len(hist[1]), 1)

    def test_handle_command_state_rejects_unknown_model(self):
        overrides = {}
        resp = core.handle_command_state(
            ("model", "ghost"),
            1,
            True,
            {},
            overrides,
            5,
            5,
            "m",
            ["real-one"],
            "h",
        )
        self.assertIsNotNone(resp)
        text = resp[0] if resp else ""
        self.assertIn("Доступные модели", text)
        self.assertEqual(overrides, {})
        self.assertFalse(resp[1] if resp else True)

    def test_handle_command_state_accepts_model_when_list_unknown(self):
        overrides = {}
        resp = core.handle_command_state(
            ("model", "anything"),
            1,
            True,
            {},
            overrides,
            5,
            5,
            "m",
            [],
            "h",
        )
        self.assertEqual(overrides[1], "anything")
        self.assertTrue(resp[1] if resp else False)

    def test_handle_command_state_model(self):
        overrides = {}
        resp = core.handle_command_state(
            ("model", "gpt"),
            1,
            True,
            {},
            overrides,
            5,
            5,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("Модель установлена / Model set: gpt", True, False))
        self.assertEqual(overrides[1], "gpt")
        resp = core.handle_command_state(
            ("model", None),
            1,
            True,
            {},
            overrides,
            5,
            5,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("Текущая модель / Current model: gpt", False, False))

    def test_handle_command_state_ping_and_history_removed(self):
        for command in (
            ("ping", None),
            ("history", None),
            ("autorespond", True),
            ("ignore", None),
        ):
            with self.subTest(command=command):
                resp = core.handle_command_state(
                    command,
                    1,
                    True,
                    {},
                    {},
                    5,
                    5,
                    "m",
                    [],
                    "h",
                )
                self.assertIsNone(resp)

    def test_handle_command_state_models_and_help(self):
        resp = core.handle_command_state(
            ("models", None),
            1,
            True,
            {},
            {},
            5,
            5,
            "m",
            ["m"],
            "h",
        )
        if resp is None:
            self.fail("resp is None")
        self.assertIn("m", resp[0])
        resp = core.handle_command_state(
            ("help", None),
            1,
            True,
            {},
            {},
            5,
            5,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("h", False, False))

    def test_handle_command_state_unknown(self):
        resp = core.handle_command_state(
            ("nope", None),
            1,
            True,
            {},
            {},
            5,
            5,
            "m",
            [],
            "h",
        )
        self.assertIsNone(resp)

    def test_append_group_history(self):
        hist = {}
        asyncio.run(core.append_group_history(1, "x", hist, 5, asyncio.Lock()))
        self.assertEqual(hist[1][0]["content"], "x")

    def test_prepare_messages(self):
        store = _StoreStub()
        store.chat_history[1] = deque([{"role": "user", "content": "x"}], maxlen=5)
        messages = asyncio.run(
            core.prepare_messages(store, 1, 5, 5, lambda _cid: "sys")
        )
        self.assertEqual(messages[0], {"role": "system", "content": "sys"})
        self.assertEqual(messages[1]["content"], "x")

    def test_make_stream_callbacks(self):
        seen = []

        def render(prefix, reasoning, tools, answer):
            return f"{prefix}|{''.join(reasoning)}|{','.join(tools)}|{answer}"

        async def edit(chat_id, msg_id, text):
            seen.append(text)

        state, _render_fn, on_delta, on_reasoning, on_tool = core.make_stream_callbacks(
            "p", render, edit, 1, 0.0
        )
        state["edit_id"] = 5

        async def run():
            await on_reasoning("r")
            await on_tool("t")
            await on_delta("a")

        asyncio.run(run())
        self.assertTrue(seen)
        self.assertIn("r", seen[-1])
        self.assertIn("t", seen[-1])
        self.assertIn("a", seen[-1])

    def test_stream_answer_self_edit(self):
        store = _StoreStub()
        edited = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            edited.append(text)

        async def reply_fn(event, text):
            return SimpleNamespace(id=1)

        def render(prefix, reasoning, tools, answer):
            return prefix + answer

        async def stream_fn(
            messages, model, chat_id, on_delta, on_reasoning, on_tool, **kwargs
        ):
            await on_delta("hi")
            return "hi"

        answer = asyncio.run(
            core.stream_answer(
                store,
                event=None,
                chat_id=1,
                is_self=True,
                messages=[],
                model="m",
                prefix="p:",
                self_edit_id=5,
                render_fn=render,
                edit_fn=edit_fn,
                reply_fn=reply_fn,
                action=_NullAsyncContext(),
                stream_fn=stream_fn,
            )
        )
        self.assertEqual(answer, "hi")
        self.assertEqual(edited[-1], "p:hi")

    def test_parse_state_data(self):
        data = {
            "model_overrides": {"1": "m"},
            "coder_chats": [1],
            "reasoning_hidden": [2],
            "tools_hidden": [3],
        }
        parsed = core.parse_state_data(data)
        self.assertEqual(parsed["model_overrides"], {1: "m"})
        self.assertEqual(parsed["coder_chats"], {1})
        self.assertEqual(parsed["reasoning_hidden"], {2})
        self.assertEqual(parsed["tools_hidden"], {3})

    def test_parse_state_data_coerces_model_to_text(self):
        parsed = core.parse_state_data({"model_overrides": {"1": 5}})
        self.assertEqual(parsed["model_overrides"], {1: "5"})

    def test_parse_state_data_tolerates_bad_members(self):
        parsed = core.parse_state_data(
            {
                "model_overrides": {"не число": "m", "7": "m7"},
                "coder_chats": "строка",
                "reasoning_hidden": ["x", 3],
                "tools_hidden": {"1", "y"},
            }
        )
        self.assertEqual(parsed["model_overrides"], {7: "m7"})
        self.assertEqual(parsed["coder_chats"], set())
        self.assertEqual(parsed["reasoning_hidden"], {3})
        self.assertEqual(parsed["tools_hidden"], {1})
        with self.assertRaises(TypeError):
            core.parse_state_data([])

    def test_inline_command_matches_ignores_empty_head(self):
        self.assertEqual(core.inline_command_matches("/@danybot", True), [])
        self.assertEqual(core.inline_command_matches("/", True), [])
        self.assertEqual(core.inline_command_matches("нет слеша", True), [])

    def test_async_saver_mark_dirty_reuses_live_task(self):
        async def scenario():
            saver = core.AsyncSaver(lambda: True, delay=0.05)
            saver.mark_dirty()
            first = cast(Any, saver._task)
            self.assertIsNotNone(first)
            saver.mark_dirty()
            saver.mark_dirty()
            self.assertIs(saver._task, first)
            await asyncio.wait_for(saver.flush(), timeout=5)
            self.assertTrue(first.done())

        asyncio.run(scenario())

    def test_async_saver_mark_dirty_without_loop(self):
        saver = core.AsyncSaver(lambda: None)
        saver.mark_dirty()
        self.assertTrue(saver._dirty)
        self.assertIsNone(saver._task)

    def test_async_saver_mark_dirty_without_loop_emits_no_warning(self):
        saver = core.AsyncSaver(lambda: None)
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            saver.mark_dirty()

    def test_async_saver_flush_propagates_cancellation(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def writer():
                started.set()
                release.wait(5)
                return True

            saver = core.AsyncSaver(writer, delay=0.0)
            saver.mark_dirty()
            flush_task = asyncio.ensure_future(saver.flush())
            for _ in range(500):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(started.is_set())
            flush_task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await flush_task
            writer_task = cast(Any, saver._task)
            self.assertIsNotNone(writer_task)
            writer_task.cancel()
            for _ in range(200):
                if writer_task.done():
                    break
                await asyncio.sleep(0.01)

        asyncio.run(scenario())

    def test_load_history_file_rejects_non_object_payload(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_history_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "history.json"
        for payload in ("[]", "5", "null", '"текст"'):
            with self.subTest(payload=payload):
                target.write_text(payload, encoding="utf-8")
                self.assertIsNone(core.load_history_file(target, 10, 5))

    def test_remember_key_respects_limit(self):
        target: set[int] = set()
        for key in range(50):
            core._remember_key(target, key, 10)
        self.assertEqual(len(target), 10)
        self.assertEqual(max(target), 49)

    def test_check_cooldown_keeps_current_chat_activity(self):
        activity = {7: 0.0, 8: 0.0}
        blocked = core.check_cooldown(
            7, 5000.0, 7200.0, activity, cleanup_threshold=1, cleanup_age=2
        )
        self.assertTrue(blocked)
        self.assertIn(7, activity)
        self.assertNotIn(8, activity)

    def test_restore_module_removes_injected_attributes(self):
        snap = _snapshot_module(userbot)
        self.assertNotIn("inline_mode", snap)
        userbot.__dict__["inline_mode"] = True
        userbot.recent_reply_ids.add((4242, 777))
        self.addCleanup(userbot.recent_reply_ids.discard, (4242, 777))
        _restore_module(userbot, snap)
        self.assertFalse(hasattr(userbot, "inline_mode"))
        self.assertNotIn((4242, 777), userbot.recent_reply_ids)

    @unittest.skipUnless(os.name == "posix", "права файла задаёт только posix")
    def test_atomic_write_keeps_file_mode(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_mode_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "state.json"
        target.write_text("{}", encoding="utf-8")
        os.chmod(target, 0o600)
        self.assertTrue(
            core.save_state_file(
                target,
                {
                    "model_overrides": {},
                    "coder_chats": set(),
                    "reasoning_hidden": set(),
                    "tools_hidden": set(),
                },
            )
        )
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in tmp.iterdir()], ["state.json"])

    def test_async_saver_logs_write_failure(self):
        def writer():
            raise OSError("no space")

        saver = core.AsyncSaver(writer, delay=0.01, logger=userbot.logger)

        async def run():
            saver.mark_dirty()
            await asyncio.sleep(0.05)
            await saver.flush()

        with self.assertLogs("danybot", level="WARNING") as captured:
            asyncio.run(run())
        self.assertTrue(captured.output)
        self.assertTrue(saver._dirty)

    def test_async_saver_cancelled_while_waiting_for_delay(self):
        calls = []
        saver = core.AsyncSaver(lambda: calls.append(1), delay=5)

        async def run():
            saver.mark_dirty()
            await asyncio.sleep(0)
            task = cast(Any, saver._task)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())
        self.assertEqual(calls, [])

    def test_async_saver_flush_cancels_stuck_write_task(self):
        calls = []

        async def write():
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(5)
            return True

        saver = core.AsyncSaver(lambda: True, delay=-9.7)
        saver._write = write

        async def run():
            saver.mark_dirty()
            await asyncio.sleep(0.02)
            await saver.flush()

        asyncio.run(run())
        self.assertEqual(calls, [1, 1])
        self.assertFalse(saver._dirty)

    def test_mode_store_rejects_unknown_attribute(self):
        store = core.ModeStore({})
        with self.assertRaises(AttributeError):
            _ = store.not_a_mode_key
        self.assertFalse(hasattr(store, "not_a_mode_key"))

    def test_save_state_from_reports_write_failure(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_statefail_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "state.json"
        target.mkdir()
        store = _StoreStub()
        with self.assertLogs("danybot", level="WARNING"):
            saved = core.save_state_from(store, target, userbot.logger)
        self.assertFalse(saved)

    def test_save_history_from_reports_write_failure(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_histfail_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "history.json"
        target.mkdir()
        store = _StoreStub()
        with self.assertLogs("danybot", level="WARNING"):
            saved = core.save_history_from(store, target, userbot.logger)
        self.assertFalse(saved)

    def test_edit_text_skips_empty_text(self):
        client = RichFakeClient()
        with self.assertLogs("danybot", level="WARNING") as captured:
            ok = asyncio.run(core.edit_text(client, 1, 2, "   ", 2, userbot.logger))
        self.assertFalse(ok)
        self.assertEqual(client.edited, [])
        self.assertIn("пустой текст", captured.output[0])

    def test_edit_text_logs_flood_retries(self):
        client = FlakyEditClient()
        client.always_flood = True
        with self.assertLogs("danybot", level="WARNING") as captured:
            ok = asyncio.run(core.edit_text(client, 1, 2, "текст", 2, userbot.logger))
        self.assertFalse(ok)
        self.assertEqual(client.attempts, 2)
        self.assertTrue(any("флуд-лимит" in line for line in captured.output))
        self.assertTrue(any("попыток" in line for line in captured.output))

    def test_edit_text_logs_error(self):
        class _Boom:
            async def edit_message(self, chat, msg_id, text, **kwargs):
                raise OSError("telegram down")

        with self.assertLogs("danybot", level="WARNING") as captured:
            ok = asyncio.run(core.edit_text(_Boom(), 1, 2, "текст", 2, userbot.logger))
        self.assertFalse(ok)
        self.assertIn("OSError", captured.output[0])

    def test_check_cooldown_cleans_stale_activity(self):
        activity = {1: 0.0, 2: 99.5}
        blocked = core.check_cooldown(
            3, 100.0, 5.0, activity, cleanup_threshold=1, cleanup_age=2
        )
        self.assertFalse(blocked)
        self.assertNotIn(1, activity)
        self.assertIn(2, activity)
        self.assertIn(3, activity)

    def test_append_message_context_skips_duplicate_key(self):
        store = _StoreStub()
        labels = []
        saved = []

        async def label():
            labels.append(1)
            return "Tester"

        def save():
            saved.append(1)

        async def run():
            for _attempt in range(2):
                await core.append_message_context(
                    store,
                    1,
                    5,
                    True,
                    "привет",
                    False,
                    True,
                    lambda text, triggered: text.strip(),
                    lambda text: text,
                    label,
                    10,
                    10,
                    save,
                )

        asyncio.run(run())
        self.assertEqual(labels, [1])
        self.assertEqual(saved, [1])
        self.assertEqual(len(store.chat_history[1]), 1)

    def test_append_message_context_releases_key_when_interrupted(self):
        store = _StoreStub()
        calls = []

        async def slow_label():
            calls.append(1)
            await asyncio.sleep(5)
            return "Tester"

        async def run():
            task = asyncio.ensure_future(
                core.append_message_context(
                    store,
                    1,
                    5,
                    True,
                    "привет",
                    False,
                    True,
                    lambda text, triggered: text.strip(),
                    lambda text: text,
                    slow_label,
                    10,
                    10,
                    lambda: None,
                )
            )
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertNotIn((1, 5), store.seen_msg_keys)
        self.assertEqual(len(store.chat_history.get(1, [])), 0)

        async def retry():
            async def label():
                return "Tester"

            await core.append_message_context(
                store,
                1,
                5,
                True,
                "привет",
                False,
                True,
                lambda text, triggered: text.strip(),
                lambda text: text,
                label,
                10,
                10,
                lambda: None,
            )

        asyncio.run(retry())
        self.assertEqual(len(store.chat_history[1]), 1)

    @staticmethod
    def _tool_messages_are_paired(trimmed) -> bool:
        for index, item in enumerate(trimmed):
            if item["role"] != "tool":
                continue
            back = index - 1
            while back >= 0 and trimmed[back]["role"] == "tool":
                back -= 1
            if back < 0:
                return False
            owner = trimmed[back]
            if owner["role"] != "assistant" or "tool_calls" not in owner:
                return False
        return True

    def test_trim_tool_history_keeps_tool_block_with_its_assistant(self):
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "a0", "tool_calls": [{"id": "0"}]},
            {"role": "tool", "content": "r0"},
            {"role": "assistant", "content": "a1", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "r1"},
            {"role": "tool", "content": "r2"},
        ]
        trimmed = core.trim_tool_history(messages, 5)
        self.assertNotIn("tool", [item["role"] for item in trimmed[:2]])
        self.assertLessEqual(len(trimmed), 5)
        self.assertTrue(self._tool_messages_are_paired(trimmed))

    def test_trim_tool_history_never_orphans_tool_messages(self):
        for rounds in (1, 2, 3, 5, 8, 13, 30):
            for calls in (1, 2, 5):
                for head_size in (1, 2):
                    for max_messages in (3, 5, 10, 40, 60):
                        with self.subTest(
                            rounds=rounds,
                            calls=calls,
                            head=head_size,
                            limit=max_messages,
                        ):
                            messages: list[dict[str, Any]] = [
                                {"role": "system", "content": "s"},
                                {"role": "user", "content": "u"},
                            ]
                            for index in range(rounds):
                                messages.append(
                                    {
                                        "role": "assistant",
                                        "content": "",
                                        "tool_calls": [
                                            {"id": f"{index}-{c}"} for c in range(calls)
                                        ],
                                    }
                                )
                                for call in range(calls):
                                    messages.append(
                                        {
                                            "role": "tool",
                                            "tool_call_id": f"{index}-{call}",
                                            "content": "r",
                                        }
                                    )
                            trimmed = core.trim_tool_history(
                                messages, max_messages, head_size=head_size
                            )
                            self.assertTrue(
                                self._tool_messages_are_paired(trimmed),
                                msg=f"сиротские tool: {trimmed}",
                            )
                            if len(messages) > max_messages >= 2:
                                self.assertTrue(
                                    len(trimmed) <= max_messages
                                    or len(trimmed) == len(messages),
                                    msg="обрезка не уложилась в лимит",
                                )

    def test_trim_tool_history_keeps_all_when_cut_is_empty(self):
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "user", "content": "v"},
        ]
        self.assertIs(core.trim_tool_history(messages, 2, head_size=2), messages)

    def test_stream_answer_sends_new_message_when_edit_fails(self):
        store = _StoreStub()
        sent = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            return False

        async def reply_fn(event, text):
            sent.append(text)
            return SimpleNamespace(id=4321)

        def render(prefix, reasoning, tools, answer):
            return answer

        async def stream_fn(*_args, **_kwargs):
            return "итог"

        with self.assertLogs("danybot", level="WARNING") as captured:
            answer = asyncio.run(
                core.stream_answer(
                    store,
                    event=None,
                    chat_id=1,
                    is_self=False,
                    messages=[],
                    model="m",
                    prefix="",
                    self_edit_id=None,
                    render_fn=render,
                    edit_fn=edit_fn,
                    reply_fn=reply_fn,
                    action=_NullAsyncContext(),
                    stream_fn=stream_fn,
                    logger=userbot.logger,
                )
            )
        self.assertEqual(answer, "итог")
        self.assertEqual(sent, ["…", "итог"])
        self.assertIn("новым сообщением", captured.output[0])
        self.assertNotIn((1, 4321), store.recent_reply_ids)

    def test_stream_answer_records_delivery_flag(self):
        store = _StoreStub()
        delivery = {}
        edited = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            edited.append(text)
            return True

        async def reply_fn(event, text):
            return SimpleNamespace(id=1)

        def render(prefix, reasoning, tools, answer):
            return answer

        async def stream_fn(*_args, **_kwargs):
            raise KeyError("broken tool")

        with self.assertRaises(KeyError):
            asyncio.run(
                core.stream_answer(
                    store,
                    event=None,
                    chat_id=1,
                    is_self=False,
                    messages=[],
                    model="m",
                    prefix="",
                    self_edit_id=None,
                    render_fn=render,
                    edit_fn=edit_fn,
                    reply_fn=reply_fn,
                    action=_NullAsyncContext(),
                    stream_fn=stream_fn,
                    delivery=delivery,
                )
            )
        self.assertTrue(delivery.get("delivered"))
        self.assertIn("Ошибка", edited[-1])

    def test_stream_answer_logs_interruption(self):
        store = _StoreStub()
        edited = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            edited.append(text)
            return True

        async def reply_fn(event, text):
            return SimpleNamespace(id=910)

        def render(prefix, reasoning, tools, answer):
            return answer

        async def scenario():
            async def stream_fn(*_args, **_kwargs):
                await asyncio.sleep(5)

            task = asyncio.ensure_future(
                core.stream_answer(
                    store,
                    event=None,
                    chat_id=1,
                    is_self=False,
                    messages=[],
                    model="m",
                    prefix="",
                    self_edit_id=None,
                    render_fn=render,
                    edit_fn=edit_fn,
                    reply_fn=reply_fn,
                    action=_NullAsyncContext(),
                    stream_fn=stream_fn,
                    logger=userbot.logger,
                )
            )
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with self.assertLogs("danybot", level="INFO") as captured:
            asyncio.run(scenario())
        self.assertEqual(edited[-1], core.INTERRUPTED_NOTICE)
        self.assertTrue(any("прерван" in line for line in captured.output))

    def test_final_text_keeps_placeholder_with_reasoning_only(self):
        state = {"reasoning_parts": ["рассуждение"], "tool_parts": []}
        self.assertEqual(core._final_text(state, lambda: "…", "", None), "…")
        state = {"reasoning_parts": [], "tool_parts": []}
        self.assertEqual(
            core._final_text(state, lambda: "", "", RuntimeError("boom")),
            core.ERROR_NOTICE,
        )


class ContractPromptTest(BotTestCase):
    CHAT_ID = 7591254790

    def setUp(self):
        super().setUp()
        tmp = Path(tempfile.mkdtemp(prefix="danybot_contract_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "capabilities.md").write_text(
            "# Capabilities\n\n## Anti-AI traces\n\n//full\n", encoding="utf-8"
        )
        (tmp / "contract.md").write_text(
            "# Контракт\n\n## Команды\n\n//check\n", encoding="utf-8"
        )
        self.addCleanup(self._reset_cache)
        saved_dir = userbot.CONTRACT_DIR
        userbot.CONTRACT_DIR = tmp
        self.addCleanup(setattr, userbot, "CONTRACT_DIR", saved_dir)
        self._reset_cache()

    def _reset_cache(self):
        userbot._contract_cache["key"] = None
        userbot._contract_cache["text"] = ""

    def test_contract_loaded_from_dir(self):
        text = userbot.load_contract()
        self.assertIn("# Capabilities", text)
        self.assertIn("# Контракт", text)
        self.assertIn("Anti-AI traces", text)
        self.assertIn("//full", text)

    def test_capabilities_alias_resolved(self):
        self.assertEqual(
            userbot._contract_status("capabilities.server.md"), "capabilities.md (ok)"
        )
        self.assertEqual(userbot._contract_status("contract.md"), "contract.md (ok)")
        self.assertEqual(userbot._contract_status("missing.md"), "missing.md (нет)")

    def test_contract_cached_until_mtime_changes(self):
        first = userbot.load_contract()
        second = userbot.load_contract()
        self.assertEqual(first, second)
        self.assertIsNotNone(userbot._contract_cache["key"])
        target = userbot.CONTRACT_DIR / "contract.md"
        target.write_text("# Контракт\n\n//fix\n", encoding="utf-8")
        self.assertIn("//fix", userbot.load_contract())

    def test_contract_can_be_disabled(self):
        saved = userbot.CONTRACT_ENABLED
        self.addCleanup(setattr, userbot, "CONTRACT_ENABLED", saved)
        userbot.CONTRACT_ENABLED = False
        self.assertEqual(userbot.load_contract(), "")

    def test_contract_only_in_coder_mode(self):
        contract = userbot.load_contract()
        self.assertTrue(contract)
        self.assertIn(
            contract,
            userbot.system_for(self.CHAT_ID, mode="coder", with_contract=True),
        )
        for mode in ("userbot", "bot", "coder"):
            with self.subTest(mode=mode):
                self.assertNotIn(contract, userbot.system_for(self.CHAT_ID, mode=mode))

    def test_contract_requires_bot_flag(self):
        contract = userbot.load_contract()
        self.assertTrue(contract)
        self.assertIn(
            contract,
            userbot.system_prompt_report(
                self.CHAT_ID, mode="coder", with_contract=True
            ),
        )
        report = userbot.system_prompt_report(self.CHAT_ID, mode="coder")
        self.assertNotIn(contract, report)

    def test_system_prompt_report_shape(self):
        report = userbot.system_prompt_report(self.CHAT_ID, mode="bot")
        self.assertIn("Режим / Mode: bot", report)
        self.assertIn("Контракт / Contract: включён", report)
        self.assertIn("capabilities.md (ok)", report)
        self.assertIn("contract.md (ok)", report)
        self.assertIn(str(userbot.CONTRACT_DIR), report)

    def test_system_prompt_report_marks_missing_files(self):
        (userbot.CONTRACT_DIR / "contract.md").unlink()
        report = userbot.system_prompt_report(self.CHAT_ID, mode="bot")
        self.assertIn("contract.md (нет)", report)
        self.assertIn("Контракт / Contract: включён", report)

    def test_prompt_aliases_parse(self):
        for text in (".db prompt", ".ai prompt"):
            with self.subTest(text=text):
                self.assertEqual(userbot.handle_commands(text), ("prompt", None))
        for text in ("/prompt",):
            with self.subTest(text=text):
                self.assertEqual(bot.handle_bot_commands(text), ("prompt", None))

    def test_prompt_reachable_from_settings_menu(self):
        data = rows_data(bot._settings_rows(self.CHAT_ID))
        self.assertIn("settings:prompt", data)
        block = menu_block()
        self.assertIn('command="settings"', block)
        self.assertNotIn('command="prompt"', block)


class ContractDirTest(BotTestCase):
    def test_env_dir_wins(self):
        with mock.patch.dict(os.environ, {"CONTRACT_DIR": "somewhere/cdn"}):
            self.assertEqual(userbot._resolve_contract_dir(), Path("somewhere/cdn"))

    def test_existing_candidate_is_used(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_cdn_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        candidates = (tmp, *userbot.CONTRACT_DIR_CANDIDATES)
        with mock.patch.dict(os.environ):
            os.environ.pop("CONTRACT_DIR", None)
            with mock.patch.object(userbot, "CONTRACT_DIR_CANDIDATES", candidates):
                self.assertEqual(userbot._resolve_contract_dir(), tmp)

    def test_missing_candidates_keep_first(self):
        candidates = (Path("/nope/cdn"),)
        with mock.patch.dict(os.environ):
            os.environ.pop("CONTRACT_DIR", None)
            with mock.patch.object(userbot, "CONTRACT_DIR_CANDIDATES", candidates):
                self.assertEqual(userbot._resolve_contract_dir(), candidates[0])

    def test_signature_survives_unreadable_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_sig_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "contract.md"
        target.write_text("правило", encoding="utf-8")
        with (
            mock.patch.object(userbot, "CONTRACT_DIR", tmp),
            mock.patch.object(userbot, "CONTRACT_FILES", ("contract.md",)),
        ):
            userbot._contract_cache.update({"key": None, "text": ""})
            before = userbot._contract_signature()
            with mock.patch.object(Path, "stat", side_effect=OSError):
                after = userbot._contract_signature()
        self.assertNotEqual(before, after)
        self.assertEqual(after[0][1], "contract.md")

    def test_load_contract_tolerates_unreadable_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_read_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "contract.md"
        target.write_text("правило", encoding="utf-8")
        with (
            mock.patch.object(userbot, "CONTRACT_DIR", tmp),
            mock.patch.object(userbot, "CONTRACT_FILES", ("contract.md",)),
        ):
            userbot._contract_cache.update({"key": None, "text": ""})
            self.assertIn("правило", userbot.load_contract())
            userbot._contract_cache.update({"key": None, "text": ""})
            with mock.patch.object(
                Path,
                "read_text",
                side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad"),
            ):
                self.assertEqual(userbot.load_contract(), "")

    def test_system_for_appends_extra_system(self):
        saved = userbot.EXTRA_SYSTEM
        self.addCleanup(setattr, userbot, "EXTRA_SYSTEM", saved)
        userbot.EXTRA_SYSTEM = "ДОПОЛНИТЕЛЬНО"
        self.assertIn("ДОПОЛНИТЕЛЬНО", userbot.system_for(1, mode="bot"))
        self.assertIn(
            "ДОПОЛНИТЕЛЬНО", userbot.system_for(1, mode="bot", with_contract=True)
        )
        userbot.EXTRA_SYSTEM = ""
        self.assertNotIn("ДОПОЛНИТЕЛЬНО", userbot.system_for(1, mode="userbot"))

    def test_system_prompt_report_truncates(self):
        report = userbot.system_prompt_report(1, mode="userbot", limit=20)
        self.assertIn("обрезано / truncated", report)


class DatacenterTest(BotTestCase):
    def setUp(self):
        super().setUp()
        for attr, value in (
            ("API_ID", 123),
            ("API_HASH", "hash"),
            ("SESSION_NAME", "danybot_dc_selftest"),
            ("BOT_TOKEN", BOT_TOKEN_VALUE),
        ):
            saved = getattr(userbot, attr)
            self.addCleanup(setattr, userbot, attr, saved)
            setattr(userbot, attr, value)

    def test_dc_address_known_and_unknown(self):
        self.assertEqual(core.dc_address(2), core.DC_FALLBACK)
        self.assertEqual(core.dc_fallback(), core.DC_FALLBACK)
        self.assertEqual(core.dc_address(9), "")
        self.assertEqual(core.dc_address(None), "")
        self.assertEqual(core.dc_address("junk"), "")
        self.assertEqual(core.dc_address("2"), core.DC_FALLBACK)
        self.assertEqual(core.dc_address(" 2 "), core.DC_FALLBACK)
        self.assertEqual(core.dc_address(2.0), core.DC_FALLBACK)
        self.assertEqual(core.dc_address(2.5), "")
        self.assertEqual(core.dc_address("2.5"), "")
        self.assertEqual(core.dc_address(True), "")
        self.assertEqual(core.dc_address(False), "")
        self.assertEqual(core.dc_address([2]), "")
        self.assertEqual(core.dc_address("+2"), "")
        self.assertEqual(core.dc_address("02"), "")

    def test_disconnect_quietly_passes_timeout_to_drain(self):
        seen: dict = {}

        class _Registry:
            def cancel_all(self, reason="", logger=None):
                return 0

            async def drain(self, timeout=core.DRAIN_TIMEOUT):
                seen["timeout"] = timeout

        for module in (bot, userbot):
            seen.clear()
            target = cast(Any, module)
            saved = target.SESSIONS
            self.addCleanup(setattr, target, "SESSIONS", saved)
            target.SESSIONS = _Registry()
            asyncio.run(module.disconnect_quietly(timeout=7))
            self.assertEqual(seen["timeout"], 7, module.__name__)

    def test_opt_int_arg_allows_unbounded_top(self):
        self.assertEqual(tools_module._opt_int_arg({}, "n", 1, None), None)
        self.assertIsNone(tools_module._opt_int_arg({"n": None}, "n", 1, None))
        self.assertEqual(tools_module._opt_int_arg({"n": 3}, "n", 1, None), 3)
        self.assertEqual(tools_module._opt_int_arg({"n": 0}, "n", 1, None), 1)
        self.assertEqual(
            tools_module._opt_int_arg({"n": 10**9}, "n", 1, None),
            tools_module.MAX_OPT_INT,
        )
        self.assertEqual(tools_module._opt_int_arg({"n": "junk"}, "n", 1, 20), 1)
        self.assertEqual(tools_module._opt_int_arg({"n": 99}, "n", 1, 20), 20)

    def test_dc_api_url(self):
        self.assertEqual(core.dc_api_url(2), core.API_BASE_URL)
        self.assertEqual(core.dc_api_url(2), f"https://{core.API_HOST}")
        self.assertIsNone(core.dc_api_url(9))
        self.assertIsNone(core.dc_api_url(None))
        self.assertIsNone(core.dc_api_url(True))
        self.assertIsNone(core.dc_api_url([2]))

    def test_dc_api_pin(self):
        self.assertEqual(core.dc_api_pin(2), core.DC_FALLBACK)
        self.assertEqual(core.dc_api_pin(4), core.DC_ADDRESSES[4])
        self.assertEqual(core.dc_api_pin(None), core.dc_fallback())
        self.assertEqual(core.dc_api_pin(9), "")
        self.assertEqual(core.dc_api_pin("junk"), "")

    def test_dc_api_pin_reads_fallback_from_env(self):
        with mock.patch.dict(os.environ, {"DC_FALLBACK": "10.0.0.9"}):
            self.assertEqual(core.dc_api_pin(None), "10.0.0.9")

    def test_dc_order_default_and_env(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("DC_ORDER", None)
            self.assertEqual(core.dc_order(), list(core.DC_ORDER))
        with mock.patch.dict(os.environ, {"DC_ORDER": "3,1,3,junk,,9"}):
            self.assertEqual(core.dc_order(), [3, 1])
        with mock.patch.dict(os.environ, {"DC_ORDER": "junk,9"}):
            self.assertEqual(core.dc_order(), list(core.DC_ORDER))

    def test_dc_candidates_start_with_main(self):
        with mock.patch.dict(os.environ):
            for name in ("DC_ORDER", "DC_DISABLED", "DC_MAIN", "DC_FALLBACK"):
                os.environ.pop(name, None)
            items = core.dc_candidates()
        self.assertEqual(items[0]["address"], core.DC_MAIN)
        self.assertEqual(items[0]["dc"], 2)
        addresses = [item["address"] for item in items]
        self.assertEqual(addresses.count(core.DC_MAIN), 1)
        self.assertEqual(addresses[1], core.DC_ADDRESSES[1])

    def test_dc_candidates_keep_configured_order_after_main(self):
        with mock.patch.dict(
            os.environ,
            {
                "DC_ORDER": "4,3",
                "DC_DISABLED": "",
                "DC_MAIN": core.DC_MAIN,
                "DC_FALLBACK": core.DC_MAIN,
            },
        ):
            items = core.dc_candidates()
        self.assertEqual(
            [item["address"] for item in items],
            [core.DC_MAIN, core.DC_ADDRESSES[4], core.DC_ADDRESSES[3]],
        )

    def test_dc_candidates_respects_disabled(self):
        with mock.patch.dict(
            os.environ,
            {
                "DC_ORDER": "1,2",
                "DC_DISABLED": "1,9",
                "DC_MAIN": core.DC_MAIN,
                "DC_FALLBACK": core.DC_MAIN,
            },
        ):
            items = core.dc_candidates()
        addresses = [item["address"] for item in items]
        self.assertNotIn(core.DC_ADDRESSES[1], addresses)
        self.assertEqual(addresses[0], core.DC_MAIN)

    def test_dc_candidates_avoid_duplicates(self):
        with mock.patch.dict(
            os.environ,
            {
                "DC_ORDER": "2",
                "DC_MAIN": core.DC_MAIN,
                "DC_FALLBACK": core.DC_ADDRESSES[3],
            },
        ):
            items = core.dc_candidates()
        addresses = [item["address"] for item in items]
        self.assertEqual(addresses.count(core.DC_ADDRESSES[3]), 1)
        self.assertEqual(addresses.count(core.DC_MAIN), 1)
        self.assertEqual(addresses, [core.DC_MAIN, core.DC_ADDRESSES[3]])

    def test_dc_candidates_accepts_extra_from_env(self):
        with mock.patch.dict(
            os.environ,
            {
                "DC_ORDER": "1",
                "DC_DISABLED": "",
                "DC_MAIN": core.DC_MAIN,
                "DC_FALLBACK": core.DC_MAIN,
            },
        ):
            items = core.dc_candidates(extra=("4", "junk", "1"))
        addresses = [item["address"] for item in items]
        self.assertEqual(
            addresses[:3], [core.DC_MAIN, core.DC_ADDRESSES[1], core.DC_ADDRESSES[4]]
        )

    def test_dc_main_is_configurable_and_comes_first(self):
        with mock.patch.dict(
            os.environ, {"DC_MAIN": "10.0.0.1", "DC_FALLBACK": "10.0.0.2"}
        ):
            self.assertEqual(core.dc_main(), "10.0.0.1")
            self.assertEqual(core.dc_fallback(), "10.0.0.2")
            items = core.dc_candidates()
        self.assertEqual(
            [item["address"] for item in items][:2], ["10.0.0.1", "10.0.0.2"]
        )

    def test_dc_main_falls_back_to_default_when_empty(self):
        with mock.patch.dict(os.environ, {"DC_MAIN": "   ", "DC_FALLBACK": ""}):
            self.assertEqual(core.dc_main(), core.DC_MAIN)
            self.assertEqual(core.dc_fallback(), core.DC_MAIN)
            self.assertEqual(core.dc_candidates()[0]["address"], core.DC_MAIN)

    def test_connect_uses_dc_api_url(self):
        userbot.BOT_TOKEN = BOT_TOKEN_VALUE
        client = bot._connect(None, 2)
        session = client.bot.session
        self.assertTrue(session.api.base.startswith(core.API_BASE_URL))
        self.assertEqual(session._connector_init["resolver"]._address, core.DC_FALLBACK)
        self.assertEqual(session._connector_init["resolver"]._host, core.API_HOST)
        asyncio.run(client.close())

    def test_connect_pins_candidate_address(self):
        userbot.BOT_TOKEN = BOT_TOKEN_VALUE
        client = bot._connect(None, 0, core.DC_ADDRESSES[4])
        session = client.bot.session
        self.assertEqual(
            session._connector_init["resolver"]._address, core.DC_ADDRESSES[4]
        )
        asyncio.run(client.close())

    def test_connect_without_dc_uses_default_api_url(self):
        userbot.BOT_TOKEN = BOT_TOKEN_VALUE
        client = bot._connect(None)
        session = client.bot.session
        self.assertNotIn(core.DC_FALLBACK, session.api.base)
        self.assertEqual(
            session._connector_init["resolver"]._address, core.dc_fallback()
        )
        asyncio.run(client.close())

    def test_connect_with_unknown_dc_keeps_system_resolver(self):
        userbot.BOT_TOKEN = BOT_TOKEN_VALUE
        client = bot._connect(None, 9)
        self.assertNotIn("resolver", client.bot.session._connector_init)
        asyncio.run(client.close())

    def test_pinned_resolver_serves_api_host(self):
        resolver = bot._PinnedResolver(core.API_HOST, "149.154.167.220")

        async def run():
            return await resolver.resolve("API.Telegram.org.", 443, socket.AF_INET)

        results = asyncio.run(run())
        self.assertEqual(
            results,
            [
                {
                    "hostname": "API.Telegram.org.",
                    "host": "149.154.167.220",
                    "port": 443,
                    "family": socket.AF_INET,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                }
            ],
        )
        asyncio.run(resolver.close())

    def test_pinned_resolver_falls_back_to_system_dns(self):
        resolver = bot._PinnedResolver(core.API_HOST, "149.154.167.220")
        seen = []

        class _FakeDefault:
            async def resolve(self, host, port=0, family=socket.AF_INET):
                seen.append((host, port, family))
                return [
                    {
                        "hostname": host,
                        "host": "1.2.3.4",
                        "port": port,
                        "family": socket.AF_INET,
                        "proto": socket.IPPROTO_TCP,
                        "flags": 0,
                    }
                ]

            async def close(self):
                seen.append("closed")

        saved = bot.DefaultResolver
        self.addCleanup(setattr, bot, "DefaultResolver", saved)
        bot.DefaultResolver = lambda *a, **k: _FakeDefault()

        results = asyncio.run(resolver.resolve("example.org", 443))
        self.assertEqual([item["host"] for item in results], ["1.2.3.4"])
        self.assertEqual(seen, [("example.org", 443, socket.AF_INET)])
        asyncio.run(resolver.close())
        self.assertEqual(seen[-1], "closed")

    def test_pinned_resolver_ignores_ipv6_requests(self):
        resolver = bot._PinnedResolver(core.API_HOST, "149.154.167.220")
        saved = bot.DefaultResolver
        self.addCleanup(setattr, bot, "DefaultResolver", saved)

        class _FakeDefault:
            async def resolve(self, _host, _port=0, family=socket.AF_INET):
                return family

        bot.DefaultResolver = lambda *a, **k: _FakeDefault()
        self.assertEqual(
            asyncio.run(resolver.resolve(core.API_HOST, 443, socket.AF_INET6)),
            socket.AF_INET6,
        )

    def _client_with_memory_session(self, dc):
        saved_client = userbot.client
        saved_make = userbot.make_session
        self.addCleanup(setattr, userbot, "client", saved_client)
        self.addCleanup(setattr, userbot, "make_session", saved_make)
        userbot.make_session = lambda _name: MemorySession()
        userbot.client = None
        cli = userbot.get_client(dc)
        self.assertIsNotNone(cli)
        return cast(Any, cli)

    def test_get_client_pins_datacenter(self):
        cli = self._client_with_memory_session(2)
        self.assertEqual(cli.session.server_address, core.DC_FALLBACK)
        self.assertEqual(cli.session.dc_id, 2)
        self.assertEqual(cli.session.port, core.DC_PORT)

    def test_get_client_without_dc_keeps_session_address(self):
        cli = self._client_with_memory_session(None)
        self.assertNotEqual(cli.session.server_address, core.DC_FALLBACK)

    def test_pin_datacenter_keeps_authorized_session(self):
        session = mock.Mock(dc_id=4, auth_key=object())
        userbot.pin_datacenter(SimpleNamespace(session=session), 2)
        session.set_dc.assert_not_called()

    def test_pin_datacenter_pins_fresh_session(self):
        session = mock.Mock(dc_id=0, auth_key=None)
        userbot.pin_datacenter(SimpleNamespace(session=session), 3)
        session.set_dc.assert_called_once_with(3, core.DC_ADDRESSES[3], core.DC_PORT)

    def test_pin_datacenter_ignores_unknown_dc(self):
        session = mock.Mock()
        userbot.pin_datacenter(SimpleNamespace(session=session), 9)
        session.set_dc.assert_not_called()
        userbot.pin_datacenter(SimpleNamespace(session=session), None)
        session.set_dc.assert_not_called()

    def test_bot_connect_all_returns_fatal_on_bad_token(self):
        userbot.BOT_TOKEN = BOT_TOKEN_VALUE
        client = _FakeAiogramClient(
            error=TelegramUnauthorizedError(_NO_METHOD, "Unauthorized")
        )
        self.enterContext(
            mock.patch.object(bot, "_connect", mock.Mock(return_value=client))
        )

        async def proxies_none():
            return [None]

        self.enterContext(mock.patch.object(bot, "_proxy_candidates", proxies_none))
        result = asyncio.run(
            bot._connect_all({"dc": 2, "address": core.DC_FALLBACK}, [None])
        )
        self.assertEqual(result, (None, True))
        self.assertEqual(client.closed, 1)

    def test_dc_candidates_skip_junk_in_disabled(self):
        with mock.patch.dict(
            os.environ,
            {
                "DC_ORDER": "1,2",
                "DC_DISABLED": "junk; 9,, 42 ,bad",
                "DC_MAIN": core.DC_MAIN,
                "DC_FALLBACK": core.DC_MAIN,
            },
        ):
            self.assertEqual(core._dc_int_set("DC_DISABLED"), set())
            items = core.dc_candidates()
        self.assertEqual(
            [item["address"] for item in items], [core.DC_MAIN, core.DC_ADDRESSES[1]]
        )


class TaskJournalTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.saves = []

    def _journal(self, limit=40):
        self.saves = []
        journal = core.TaskJournal(lambda: self.saves.append(1), limit=limit)
        return journal

    def test_begin_finish_and_report(self):
        journal = self._journal()
        journal.begin(5, "починить тест", model="m", coder=True)
        self.assertEqual((journal.get(5) or {}).get("status"), core.TASK_RUNNING)
        self.assertIn("В работе", journal.report(5))
        journal.progress(5, rounds=4, tools=7)
        self.assertEqual((journal.get(5) or {}).get("tools"), 7)
        journal.finish(5, core.TASK_DONE)
        record = journal.get(5) or {}
        self.assertEqual(record.get("status"), core.TASK_DONE)
        self.assertIn("Завершена", journal.report(5))
        self.assertIn("Раундов: 4", journal.report(5))
        self.assertTrue(self.saves)

    def test_progress_without_task_is_ignored(self):
        journal = self._journal()
        journal.progress(1, rounds=2)
        self.assertIsNone(journal.get(1))
        self.assertFalse(self.saves)

    def test_stale_task_cannot_overwrite_new_one(self):
        journal = self._journal()
        old = journal.begin(4, "старая", model="m")
        new = journal.begin(4, "новая", model="m")
        self.assertNotEqual(old["token"], new["token"])
        journal.finish(4, core.TASK_INTERRUPTED, reason="поздно", token=old["token"])
        record = journal.get(4) or {}
        self.assertEqual(record.get("status"), core.TASK_RUNNING)
        self.assertEqual(record.get("prompt"), "новая")
        journal.progress(4, rounds=9, token=old["token"])
        self.assertEqual((journal.get(4) or {}).get("rounds"), 0)
        journal.finish(4, core.TASK_DONE, token=new["token"])
        self.assertEqual((journal.get(4) or {}).get("status"), core.TASK_DONE)

    def test_restore_keeps_tokens_ahead_of_new_tasks(self):
        journal = self._journal()
        journal.begin(6, "из файла", model="m")
        saved = journal.snapshot()
        fresh = self._journal()
        fresh.restore(saved)
        restored_token = int((fresh.get(6) or {}).get("token") or 0)
        self.assertGreaterEqual(restored_token, 1)
        record = fresh.begin(6, "новая")
        self.assertGreater(int(record["token"]), restored_token)

    def test_progress_with_persist_saves_once(self):
        journal = self._journal()
        journal.begin(3, "задача", model="m")
        self.saves.clear()
        journal.progress(3, persist=True, rounds=2, tools=1)
        self.assertEqual(self.saves, [1])
        self.assertEqual((journal.get(3) or {}).get("rounds"), 2)
        journal.progress(3, rounds=4, tools=2)
        self.assertEqual(self.saves, [1])
        self.assertEqual((journal.get(3) or {}).get("tools"), 2)

    def test_finish_without_task_returns_none(self):
        journal = self._journal()
        self.assertIsNone(journal.finish(9, core.TASK_DONE))
        self.assertFalse(self.saves)

    def test_restore_skips_chat_ids_that_are_not_numbers(self):
        journal = self._journal()
        data = {"abc": {"status": "done"}, "7": {"status": "done"}}
        self.assertEqual(journal.restore(data), 1)
        self.assertIsNone(journal.get(None))
        self.assertIsNotNone(journal.get(7))

    def test_restore_drops_non_finite_timestamps(self):
        journal = self._journal()
        self.assertEqual(
            journal.restore(
                {"1": {"status": "done", "started": "inf", "updated": "nan"}}
            ),
            1,
        )
        record = journal.get(1) or {}
        self.assertEqual(record.get("started"), 0.0)
        self.assertEqual(record.get("updated"), 0.0)

    def test_report_marks_unreadable_time(self):
        self.assertEqual(core._fmt_time(0), "?")
        self.assertEqual(core._fmt_time(None), "?")
        self.assertEqual(core._fmt_time(1e30), "?")

    def test_report_for_unknown_chat(self):
        journal = self._journal()
        self.assertEqual(journal.report(9), "Задач в этом чате не было.")

    def test_stop_reason_and_counters_in_settings_label(self):
        journal = self._journal()
        journal.begin(1, "задача", model="m")
        journal.finish(
            1, core.TASK_STOPPED, reason="лимит раундов", rounds=40, tools=39
        )
        label = core.task_state_label(journal.get(1))
        self.assertIn("лимиту", label)
        self.assertIn("40 раундов", label)
        self.assertEqual(core.task_state_label(None), "нет")

    def test_restore_marks_running_as_interrupted(self):
        journal = self._journal()
        journal.begin(3, "долгая задача", model="m")
        data = journal.snapshot()
        fresh = self._journal()
        self.assertEqual(fresh.restore(data), 1)
        record = fresh.get(3) or {}
        self.assertEqual(record.get("status"), core.TASK_INTERRUPTED)
        self.assertIn("перезапущен", record.get("reason", ""))

    def test_restore_survives_broken_payload(self):
        journal = self._journal()
        self.assertEqual(journal.restore("broken"), 0)
        self.assertEqual(journal.restore(None), 0)
        self.assertEqual(
            journal.restore({"x": "нет", "1": {"status": "выдумка"}, "2": None}), 0
        )
        self.assertEqual(journal.restore({"1": {"status": "done", "rounds": "7"}}), 1)
        self.assertEqual((journal.get(1) or {}).get("rounds"), 7)
        self.assertEqual((journal.get(1) or {}).get("tools"), 0)
        rows = [{"chat_id": 4, "status": "interrupted", "prompt": "из списка"}]
        self.assertEqual(journal.restore(rows), 1)
        self.assertIn("из списка", journal.report(4))

    def test_prompt_and_reason_are_trimmed(self):
        journal = self._journal()
        journal.begin(1, "x" * 5000, model="m" * 500)
        record = journal.get(1) or {}
        self.assertEqual(len(record.get("prompt", "")), core.TASK_PROMPT_CHARS)
        self.assertEqual(len(record.get("model", "")), 80)
        journal.finish(1, core.TASK_STOPPED, reason="y" * 5000)
        self.assertEqual(
            len((journal.get(1) or {}).get("reason", "")), core.TASK_REASON_CHARS
        )

    def test_oldest_records_are_trimmed(self):
        journal = self._journal(limit=3)
        for chat_id in range(5):
            journal.begin(chat_id, f"задача {chat_id}")
            journal.finish(chat_id, core.TASK_DONE)
        self.assertEqual(len(journal.snapshot()), 3)
        self.assertIsNone(journal.get(0))
        self.assertIsNotNone(journal.get(4))

    def test_trim_keeps_running_records(self):
        journal = self._journal(limit=2)
        journal.begin(1, "в работе")
        journal.begin(2, "в работе")
        journal.finish(2, core.TASK_DONE)
        journal.begin(3, "третья")
        journal.finish(3, core.TASK_DONE)
        running = journal.get(1) or {}
        dropped = journal.get(2)
        latest = journal.get(3) or {}
        self.assertEqual(running["status"], core.TASK_RUNNING)
        self.assertIsNone(dropped)
        self.assertEqual(latest["status"], core.TASK_DONE)

    def test_trim_drops_running_only_when_needed(self):
        journal = self._journal(limit=1)
        journal.begin(1, "первая")
        journal.begin(2, "вторая")
        self.assertEqual(journal.get(1), None)
        self.assertIsNotNone(journal.get(2))

    def test_recent_orders_by_update_time(self):
        journal = self._journal()
        for chat_id in range(3):
            journal.begin(chat_id, f"задача {chat_id}")
        journal.progress(1, rounds=1)
        recent = journal.recent(2)
        self.assertEqual([row["prompt"] for row in recent], ["задача 1", "задача 2"])

    def test_state_file_keeps_tasks(self):
        for mod in (userbot, bot):
            self.assertIn("danybot_tests_", str(mod.STATE_FILE.parent))
            self.assertNotEqual(mod.STATE_FILE.parent, PROJECT_DIR)
        userbot.TASKS.begin(21, "задача владельца", model="m", coder=True)
        userbot.save_state()
        data = json.loads(userbot.STATE_FILE.read_text(encoding="utf-8"))
        self.assertIn("21", data["tasks"])
        self.assertEqual(data["tasks"]["21"]["status"], core.TASK_RUNNING)
        userbot.TASKS.restore({})
        userbot.load_state()
        self.assertEqual(
            (userbot.TASKS.get(21) or {}).get("status"), core.TASK_INTERRUPTED
        )
        self.assertIn("перезапущен", (userbot.TASKS.get(21) or {}).get("reason", ""))
        userbot.TASKS.restore({})

    def test_load_state_tolerates_missing_journal_key(self):
        userbot.STATE_FILE.write_text(
            json.dumps(
                {
                    "model_overrides": {"1": "m"},
                    "coder_chats": [1],
                    "reasoning_hidden": [],
                    "tools_hidden": [],
                }
            ),
            encoding="utf-8",
        )
        userbot.load_state()
        self.assertEqual(userbot.model_overrides, {1: "m"})
        self.assertEqual(userbot.coder_chats, {1})

    def test_command_state_reports_task(self):
        out = core.handle_command_state(
            ("task", None),
            1,
            True,
            {},
            {},
            10,
            10,
            "m",
            [],
            "help",
            task_text="Задача: Завершена",
        )
        self.assertIsNotNone(out)
        self.assertEqual(cast(Any, out)[0], "Задача: Завершена")
        empty = core.handle_command_state(
            ("task", None), 1, True, {}, {}, 10, 10, "m", [], "help"
        )
        self.assertIsNotNone(empty)
        self.assertIn("Журнала задач", cast(Any, empty)[0])


class SessionRegistryTest(BotTestCase):
    def test_start_cancels_previous_in_same_chat(self):
        registry = core.SessionRegistry()
        results = {}

        async def scenario():
            async def slow():
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    results["slow"] = "cancelled"
                    raise

            async def fast():
                results["fast"] = "done"

            first = registry.start(7, slow())
            await asyncio.sleep(0)
            second = registry.start(7, fast())
            await second
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(registry.running_chats(), 0)
            self.assertFalse(registry.is_running(7))

        asyncio.run(scenario())
        self.assertEqual(results, {"slow": "cancelled", "fast": "done"})

    def test_other_chats_are_not_touched(self):
        registry = core.SessionRegistry()

        async def scenario():
            async def waiter():
                await asyncio.sleep(0)

            first = registry.start(1, waiter())
            second = registry.start(2, waiter())
            await asyncio.gather(first, second)
            self.assertFalse(registry.is_running(1))
            self.assertFalse(registry.is_running(2))
            self.assertEqual(registry.running_chats(), 0)

        asyncio.run(scenario())

    def test_cancel_and_cancel_all(self):
        registry = core.SessionRegistry()
        outcomes = []

        async def scenario():
            async def waiter():
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    outcomes.append("cancelled")
                    raise

            registry.start(1, waiter())
            registry.start(2, waiter())
            await asyncio.sleep(0)
            self.assertFalse(registry.cancel(9))
            self.assertTrue(registry.cancel(1))
            self.assertEqual(registry.running_chats(), 1)
            self.assertEqual(registry.cancel_all(), 1)
            await asyncio.sleep(0)

        asyncio.run(scenario())
        self.assertEqual(outcomes, ["cancelled", "cancelled"])

    def test_is_own_cancellation(self):
        async def scenario():
            self.assertFalse(core.is_own_cancellation())

        asyncio.run(scenario())

        async def outer():
            observed = []

            async def waiter():
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    observed.append(core.is_own_cancellation())
                    raise

            task = asyncio.ensure_future(waiter())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(observed, [True])

        asyncio.run(outer())

    def test_cancel_ignores_already_finished_task(self):
        registry = core.SessionRegistry()

        async def scenario():
            async def quick():
                return 1

            registry.start(1, quick())
            await asyncio.sleep(0)
            self.assertFalse(registry.cancel(1))
            self.assertEqual(registry.running_chats(), 0)
            await asyncio.sleep(0)
            self.assertFalse(registry.is_running(1))

        asyncio.run(scenario())
        self.assertEqual(registry.running_chats(), 0)

    def test_cancel_reaches_owner_run_while_other_is_queued(self):
        registry = core.SessionRegistry()
        state = []

        async def scenario():
            async def owner_run():
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    state.append("owner-cancelled")
                    raise

            async def queued_run():
                state.append("queued-ran")

            owner_task = registry.start(4, owner_run(), scope="owner")
            await asyncio.sleep(0)
            queued_task = registry.start(4, queued_run(), scope="other")
            await asyncio.sleep(0)
            self.assertTrue(registry.is_running(4))
            self.assertTrue(registry.cancel(4, reason="стоп"))
            with self.assertRaises(asyncio.CancelledError):
                await owner_task
            with self.assertRaises(asyncio.CancelledError):
                await queued_task
            await asyncio.sleep(0)
            self.assertFalse(registry.is_running(4))
            self.assertEqual(registry.running_chats(), 0)

        asyncio.run(scenario())
        self.assertEqual(state, ["owner-cancelled"])

    def test_cancel_all_reaches_owner_run_behind_queue(self):
        registry = core.SessionRegistry()
        state = []

        async def scenario():
            async def owner_run():
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    state.append("owner-cancelled")
                    raise

            async def queued_run():
                state.append("queued-ran")

            owner_task = registry.start(4, owner_run(), scope="owner")
            await asyncio.sleep(0)
            registry.start(4, queued_run(), scope="other")
            await asyncio.sleep(0)
            self.assertEqual(registry.cancel_all(), 1)
            with self.assertRaises(asyncio.CancelledError):
                await owner_task
            await asyncio.sleep(0)
            self.assertEqual(registry.running_chats(), 0)

        asyncio.run(scenario())
        self.assertEqual(state, ["owner-cancelled"])

    def test_drain_waits_for_cancelled_sessions(self):
        registry = core.SessionRegistry()
        done = []

        async def scenario():
            async def slow():
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.01)
                    done.append(1)
                    raise

            registry.start(4, slow(), scope="owner")
            await asyncio.sleep(0)
            registry.cancel_all()
            await registry.drain()

        asyncio.run(scenario())
        self.assertEqual(done, [1])

    def test_queued_coroutine_is_closed_when_dropped(self):
        registry = core.SessionRegistry()

        async def scenario():
            async def owner_run():
                await asyncio.sleep(5)

            async def never_runs():
                return 1

            coro = never_runs()
            registry.start(4, owner_run(), scope="owner")
            await asyncio.sleep(0)
            task = registry.start(4, coro, scope="other")
            await asyncio.sleep(0)
            registry.cancel(4)
            with self.assertRaises(asyncio.CancelledError):
                await task
            registry.cancel(4)

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            asyncio.run(scenario())

    def test_other_scope_resets_after_owner_run(self):
        registry = core.SessionRegistry()
        log = []

        async def scenario():
            async def run(tag, delay):
                try:
                    await asyncio.sleep(delay)
                    log.append(tag)
                except asyncio.CancelledError:
                    log.append(f"{tag}-cancelled")
                    raise

            registry.start(1, run("owner", 0.05), scope="owner")
            await asyncio.sleep(0.01)
            second = registry.start(1, run("b", 5.0), scope="other")
            await asyncio.sleep(0.06)
            third = registry.start(1, run("c", 0.0), scope="other")
            await asyncio.wait_for(third, timeout=1.0)
            with self.assertRaises(asyncio.CancelledError):
                await second

        asyncio.run(scenario())
        self.assertEqual(log, ["owner", "b-cancelled", "c"])

    def test_history_for_keeps_deque_maxlen_authoritative(self):
        history: dict[int, deque] = {1: deque(maxlen=5)}
        core.history_for(history, 1, 5)
        self.assertIs(history[1].maxlen, 5)
        refreshed = core.history_for(history, 1, 9)
        self.assertIs(history[1], refreshed)
        self.assertEqual(refreshed.maxlen, 9)
        core.history_for(history, 1, 9)
        self.assertIs(history[1], refreshed)

    def test_history_limit_picks_dm_or_group(self):
        self.assertEqual(core.history_limit(1, True, 100, 40), 100)
        self.assertEqual(core.history_limit(-1, False, 100, 40), 40)
        self.assertEqual(core.history_limit(1, None, 100, 40), 100)
        self.assertEqual(core.history_limit(-1, None, 100, 40), 40)

    def test_stream_answer_marks_interruption_with_partial_text(self):
        store = _StoreStub()
        edited = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            edited.append(text)
            return True

        async def reply_fn(event, text):
            return SimpleNamespace(id=901)

        def render(prefix, reasoning, tools, answer):
            return answer

        async def scenario():
            async def stream_fn(_m, _model, _chat, on_delta, *_args, **_kwargs):
                await on_delta("успел написать")
                await asyncio.sleep(5)

            task = asyncio.ensure_future(
                core.stream_answer(
                    store,
                    event=None,
                    chat_id=1,
                    is_self=False,
                    messages=[],
                    model="m",
                    prefix="",
                    self_edit_id=None,
                    render_fn=render,
                    edit_fn=edit_fn,
                    reply_fn=reply_fn,
                    action=_NullAsyncContext(),
                    stream_fn=stream_fn,
                )
            )
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertEqual(edited[-1], f"успел написать\n\n{core.INTERRUPTED_NOTICE}")
        self.assertNotIn((1, 901), store.recent_reply_ids)

    def test_stream_answer_marks_bare_interruption(self):
        store = _StoreStub()
        edited = []

        async def edit_fn(chat_id, msg_id, text, logger=None):
            edited.append(text)
            return True

        async def reply_fn(event, text):
            return SimpleNamespace(id=902)

        def render(prefix, reasoning, tools, answer):
            return answer

        async def scenario():
            async def stream_fn(*_args, **_kwargs):
                await asyncio.sleep(5)

            task = asyncio.ensure_future(
                core.stream_answer(
                    store,
                    event=None,
                    chat_id=1,
                    is_self=False,
                    messages=[],
                    model="m",
                    prefix="",
                    self_edit_id=None,
                    render_fn=render,
                    edit_fn=edit_fn,
                    reply_fn=reply_fn,
                    action=_NullAsyncContext(),
                    stream_fn=stream_fn,
                )
            )
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertEqual(edited[-1], core.INTERRUPTED_NOTICE)

    def test_disconnect_quietly_cancels_sessions(self):
        async def scenario():
            async def waiter():
                await asyncio.sleep(5)

            for mod in (userbot, bot):
                mod.SESSIONS.start(1, waiter())
            self.assertEqual(userbot.SESSIONS.running_chats(), 1)
            self.assertEqual(bot.SESSIONS.running_chats(), 1)
            await userbot.disconnect_quietly()
            await bot.disconnect_quietly()
            self.assertEqual(userbot.SESSIONS.running_chats(), 0)
            self.assertEqual(bot.SESSIONS.running_chats(), 0)
            await asyncio.sleep(0)

        asyncio.run(scenario())


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_memory_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for attr, value in (
            ("DATA_DIR", tmp),
            ("DB_PATH", tmp / "memory.db"),
        ):
            saved = getattr(memory, attr)
            self.addCleanup(setattr, memory, attr, saved)
            setattr(memory, attr, value)

    def test_remember_creates_and_updates(self):
        created = memory.remember(1, "k", "v", ["a", "a", "b"])
        self.assertTrue(created["ok"])
        self.assertEqual(created["action"], "created")
        updated = memory.remember(1, "k", "v2", "a;b")
        self.assertEqual(updated["action"], "updated")
        item = memory.recall(1, "k")["items"][0]
        self.assertEqual(item["value"], "v2")
        self.assertEqual(item["tags"], ["a", "b"])

    def test_remember_rejects_empty(self):
        self.assertFalse(memory.remember(1, "  ", "v")["ok"])
        self.assertFalse(memory.remember(1, "k", "")["ok"])

    def test_recall_search_and_limit(self):
        memory.remember(1, "alpha", "hello world", "x")
        memory.remember(1, "beta", "goodbye", "y")
        self.assertEqual(memory.recall(1, "missing")["count"], 0)
        self.assertEqual(memory.recall(1, query="hello")["count"], 1)
        self.assertEqual(memory.recall(1, query="o")["count"], 2)
        self.assertEqual(memory.recall(1, limit=1)["count"], 1)
        self.assertEqual(len(memory.recall(1, limit=0)["items"]), 2)
        self.assertEqual(len(memory.recall(1, limit=10**6)["items"]), 2)

    def test_recall_is_chat_scoped(self):
        memory.remember(1, "key", "one")
        memory.remember(2, "key", "two")
        self.assertEqual(memory.recall(2, "key")["items"][0]["value"], "two")
        self.assertEqual(memory.recall(3, "key")["count"], 0)

    def test_forget_by_key_and_id(self):
        memory.remember(1, "a", "1")
        second = memory.remember(1, "b", "2")
        self.assertFalse(memory.forget(1)["ok"])
        self.assertEqual(memory.forget(1, "a")["deleted"], 1)
        self.assertEqual(memory.forget(1, mem_id=second["id"])["deleted"], 1)
        self.assertEqual(memory.list_memories(1)["count"], 0)

    def test_list_and_stats(self):
        memory.remember(1, "a", "1")
        memory.remember(2, "b", "2")
        self.assertEqual(memory.list_memories(1)["count"], 1)
        self.assertEqual(memory.list_memories(1, limit=1)["count"], 1)
        stats = memory.stats(1)
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["chat_id"], 1)
        self.assertIn("memory.db", stats["db"])
        self.assertEqual(json.loads(memory.dumps({"a": "б"})), {"a": "б"})

    def test_limits_are_applied(self):
        memory.remember(1, "k" * 50, "v" * 50)
        item = memory.list_memories(1)["items"][0]
        self.assertEqual(len(item["key"]), 50)
        self.assertEqual(len(item["value"]), 50)

    def test_tags_are_normalized(self):
        memory.remember(1, "k", "v", ["a", "a", "b"])
        self.assertEqual(memory.list_memories(1)["items"][0]["tags"], ["a", "b"])
        self.assertEqual(memory._clean_tags(None), "")
        self.assertEqual(memory._clean_tags(" a , b ; a "), "a,b")
        self.assertEqual(memory._tags_list(""), [])
        self.assertEqual(memory._clean_tags(["t" * 600]), "")

    def test_schema_is_restored_when_table_is_gone(self):
        memory.remember(1, "k", "v")
        self.assertIn(str(memory.DB_PATH), memory._initialized)
        conn = sqlite3.connect(str(memory.DB_PATH))
        try:
            conn.execute("DROP TABLE memories")
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(memory.remember(1, "k2", "v2")["ok"])
        self.assertEqual(memory.recall(1, "k2")["items"][0]["value"], "v2")
        self.assertEqual(memory.list_memories(1)["count"], 1)

    def test_has_table_reports_sqlite_error(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE probe (id INTEGER)")
        conn.commit()
        self.assertTrue(memory._has_table(conn, "probe"))
        self.assertFalse(memory._has_table(conn, "нет_такой_таблицы"))

    def test_has_table_returns_false_on_broken_connection(self):
        conn = sqlite3.connect(":memory:")
        conn.close()
        self.assertFalse(memory._has_table(conn, "memories"))

    def test_connect_creates_missing_data_dir(self):
        target = memory.DATA_DIR / "вложенный" / "каталог"
        saved = (memory.DATA_DIR, memory.DB_PATH)
        self.addCleanup(setattr, memory, "DATA_DIR", saved[0])
        self.addCleanup(setattr, memory, "DB_PATH", saved[1])
        memory.DATA_DIR = target
        memory.DB_PATH = target / "memory.db"
        memory._initialized.clear()
        conn = memory._connect()
        self.addCleanup(conn.close)
        self.assertTrue(target.is_dir())
        self.assertTrue(memory._has_table(conn, "memories"))

    def test_connect_propagates_schema_failure(self):
        with mock.patch.object(memory.sqlite3, "connect") as connect:
            instance = connect.return_value
            instance.execute.side_effect = sqlite3.OperationalError("read only")
            with self.assertRaises(sqlite3.OperationalError):
                memory._connect()
            instance.close.assert_called()


class SkillsStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_skills_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for attr, value in (
            ("DATA_DIR", tmp),
            ("DB_PATH", tmp / "skills.db"),
        ):
            saved = getattr(skills, attr)
            self.addCleanup(setattr, skills, attr, saved)
            setattr(skills, attr, value)

    def test_save_creates_and_updates(self):
        created = skills.save_skill("n", "d", "b", "t")
        self.assertEqual(created["action"], "created")
        self.assertEqual(skills.save_skill("n", "d2", "b2")["action"], "updated")
        skill = skills.load_skill("n", touch=False)["skill"]
        self.assertEqual(skill["description"], "d2")
        self.assertEqual(skill["body"], "b2")

    def test_save_rejects_empty_name(self):
        self.assertFalse(skills.save_skill(" ", "d", "b")["ok"])

    def test_prune_keeps_just_saved_skill(self):
        saved_limit = skills.MAX_SKILLS
        self.addCleanup(setattr, skills, "MAX_SKILLS", saved_limit)
        skills.MAX_SKILLS = 3
        for index in range(3):
            skills.save_skill(f"s{index}", "d", "b")
            skills.load_skill(f"s{index}")
        result = skills.save_skill("fresh", "d", "b")
        self.assertTrue(result["ok"])
        self.assertTrue(skills.load_skill("fresh", touch=False)["ok"])
        self.assertEqual(skills.stats()["count"], 4)

    def test_clean_tags_normalizes_list_and_budget(self):
        self.assertEqual(
            skills._tags_list(skills._clean_tags(["x,y", "z"])), ["x y", "z"]
        )
        self.assertEqual(
            memory._tags_list(memory._clean_tags(["x,y", "z"])), ["x y", "z"]
        )
        long_first = "a" * (skills.MAX_TAGS + 10)
        self.assertEqual(skills._clean_tags([long_first, "short"]), "short")
        self.assertEqual(memory._clean_tags([long_first, "short"]), "short")

    def test_load_missing_and_touch(self):
        self.assertFalse(skills.load_skill("none")["ok"])
        self.assertFalse(skills.load_skill("")["ok"])
        skills.save_skill("n", "d", "b")
        skills.load_skill("n")
        self.assertEqual(skills.load_skill("n", touch=False)["skill"]["uses"], 1)

    def test_load_reports_incremented_uses(self):
        skills.save_skill("n", "d", "b")
        self.assertEqual(skills.load_skill("n")["skill"]["uses"], 1)
        self.assertEqual(skills.load_skill("n")["skill"]["uses"], 2)
        self.assertEqual(skills.load_skill("n", touch=False)["skill"]["uses"], 2)
        self.assertEqual(skills.load_skill("n", touch=False)["skill"]["uses"], 2)

    def test_tags_budget_never_splits_a_tag(self):
        long_tag = "t" * 600
        self.assertEqual(skills._clean_tags([long_tag]), "")
        self.assertEqual(skills._clean_tags(["ab", long_tag]), "ab")
        self.assertEqual(skills._clean_tags(["ab", "cd", long_tag]), "ab,cd")
        self.assertEqual(skills._clean_tags(["a" * 499, "b"]), "a" * 499)

    def test_list_filters(self):
        skills.save_skill("alpha", "first", "body one", "x")
        skills.save_skill("beta", "second", "body two", "y")
        self.assertEqual(skills.list_skills()["count"], 2)
        self.assertEqual(skills.list_skills(tag="x")["count"], 1)
        self.assertEqual(skills.list_skills(query="two")["count"], 1)
        self.assertEqual(skills.list_skills(tag="x", query="two")["count"], 0)
        self.assertEqual(skills.list_skills(limit=1)["count"], 1)
        self.assertEqual(skills.list_skills(limit=0)["count"], 2)
        self.assertNotIn("body", skills.list_skills()["items"][0])

    def test_delete_and_stats(self):
        skills.save_skill("n", "d", "b")
        self.assertEqual(skills.stats()["count"], 1)
        self.assertFalse(skills.delete_skill("")["ok"])
        self.assertEqual(skills.delete_skill("n")["deleted"], 1)
        self.assertEqual(skills.delete_skill("n")["deleted"], 0)
        self.assertIn("skills.db", skills.stats()["db"])
        self.assertEqual(json.loads(skills.dumps({"a": "б"})), {"a": "б"})

    def test_limits_are_applied(self):
        skills.save_skill("n", "d" * 20, "b" * 20)
        skill = skills.load_skill("n", touch=False)["skill"]
        self.assertEqual(len(skill["description"]), 20)
        self.assertEqual(len(skill["body"]), 20)

    def test_tags_are_normalized(self):
        skills.save_skill("n", "d", "b", ["a", "a", "b"])
        self.assertEqual(
            skills.load_skill("n", touch=False)["skill"]["tags"], ["a", "b"]
        )
        self.assertEqual(skills._clean_tags(None), "")
        self.assertEqual(skills._clean_tags(" a , b ; a "), "a,b")
        self.assertEqual(skills._tags_list(""), [])

    def test_schema_is_restored_when_table_is_gone(self):
        skills.save_skill("n", "d", "b")
        self.assertIn(str(skills.DB_PATH), skills._initialized)
        conn = sqlite3.connect(str(skills.DB_PATH))
        try:
            conn.execute("DROP TABLE skills")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(skills.save_skill("n2", "d", "b")["action"], "created")
        self.assertTrue(skills.load_skill("n2", touch=False)["ok"])
        self.assertEqual(skills.stats()["count"], 1)

    def test_has_table_reports_sqlite_error(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE probe (id INTEGER)")
        conn.commit()
        self.assertTrue(skills._has_table(conn, "probe"))
        self.assertFalse(skills._has_table(conn, "нет_такой_таблицы"))

    def test_has_table_returns_false_on_broken_connection(self):
        conn = sqlite3.connect(":memory:")
        conn.close()
        self.assertFalse(skills._has_table(conn, "skills"))

    def test_connect_creates_missing_data_dir(self):
        target = skills.DATA_DIR / "вложенный" / "каталог"
        saved = (skills.DATA_DIR, skills.DB_PATH)
        self.addCleanup(setattr, skills, "DATA_DIR", saved[0])
        self.addCleanup(setattr, skills, "DB_PATH", saved[1])
        skills.DATA_DIR = target
        skills.DB_PATH = target / "skills.db"
        skills._initialized.clear()
        conn = skills._connect()
        self.addCleanup(conn.close)
        self.assertTrue(target.is_dir())
        self.assertTrue(skills._has_table(conn, "skills"))

    def test_connect_propagates_schema_failure(self):
        with mock.patch.object(skills.sqlite3, "connect") as connect:
            instance = connect.return_value
            instance.execute.side_effect = sqlite3.OperationalError("read only")
            with self.assertRaises(sqlite3.OperationalError):
                skills._connect()
            instance.close.assert_called()


_HUMAN = SimpleNamespace(is_bot=False, id=99, first_name="Tester", last_name="")
_MACHINE = SimpleNamespace(is_bot=True, id=99, first_name="Bot", last_name="")
_BOT_SELF_ID = 42
_VIA_BOT = SimpleNamespace(id=_BOT_SELF_ID, is_bot=True, username="danybot_bot")


class _FakeMessage:
    def __init__(
        self,
        text,
        msg_id=1,
        out=False,
        is_reply=False,
        reply_msg=None,
        from_user=_HUMAN,
        via_bot=None,
    ):
        self.message = text
        self.id = msg_id
        self.out = out
        self.is_reply = is_reply
        self._reply_msg = reply_msg
        self.sender_id = 1
        self.chat_id = 1
        self.from_user = from_user
        self.via_bot = via_bot

    async def get_reply_message(self):
        if self._reply_msg is None:
            raise RPCError(request=None, message="no reply")
        return self._reply_msg


class _FakeHandlerEvent:
    def __init__(self, message, chat_id=1, sender_id=1, is_private=False):
        self.message: Any = message
        self.chat_id: Any = chat_id
        self.sender_id: Any = sender_id
        self.is_private = is_private
        self.sent = []
        self._next_id = 1000

    async def reply(self, text, buttons=None):
        self.sent.append(text)
        self._next_id += 1
        return SimpleNamespace(id=self._next_id)

    async def get_sender(self):
        return SimpleNamespace(first_name="Tester", last_name="", username="tester")


class _FakeBotClient:
    def __init__(self):
        self.typing = []

    def action(self, chat_id, action):
        self.typing.append((chat_id, action))

        async def _noop():
            return True

        return _noop


class _FakeSaver:
    def __init__(self):
        self.dirty = 0
        self.flushed = 0

    def mark_dirty(self):
        self.dirty += 1

    async def flush(self):
        self.flushed += 1


class BotHandlerTest(BotTestCase):
    GROUP = -1001
    OWNER = 5

    def setUp(self):
        super().setUp()
        for mod, attr, value in (
            (bot, "coder_chats", set()),
            (bot, "reasoning_hidden", set()),
            (bot, "tools_hidden", set()),
            (bot, "recent_reply_ids", set()),
            (bot, "seen_msg_keys", set()),
            (bot, "last_chat_activity", {}),
            (bot, "chat_history", {}),
        ):
            saved = getattr(mod, attr)
            self.addCleanup(setattr, mod, attr, saved)
            setattr(mod, attr, value)
        for attr, value in (
            ("OWNER_IDS", {self.OWNER}),
            ("ENABLE_USERBOT", False),
            ("COOLDOWN", 0),
        ):
            saved = getattr(userbot, attr)
            self.addCleanup(setattr, userbot, attr, saved)
            setattr(userbot, attr, value)
        saved_username = bot.bot_username
        self.addCleanup(setattr, bot, "bot_username", saved_username)
        bot.bot_username = "danybot"
        saved_bot_id = bot.bot_id
        self.addCleanup(setattr, bot, "bot_id", saved_bot_id)
        bot.bot_id = _BOT_SELF_ID
        bot.inline_seen.clear()
        self.addCleanup(bot.inline_seen.clear)
        self.saver = _FakeSaver()
        for attr, value in (("HISTORY_SAVER", self.saver),):
            saved = getattr(bot, attr)
            self.addCleanup(setattr, bot, attr, saved)
            setattr(bot, attr, value)
        self.client = _FakeBotClient()
        self.stream_calls = []

        async def fake_stream(*args, **kwargs):
            self.stream_calls.append((args, kwargs))
            return "ответ"

        for target, attr, value in (
            (core, "stream_answer", fake_stream),
            (bot, "get_bot_client", lambda: self.client),
        ):
            saved = getattr(target, attr)
            self.addCleanup(setattr, target, attr, saved)
            setattr(target, attr, value)

    def _run(self, event):
        asyncio.run(bot.handler(event))
        return event

    def _group_event(self, text, **kwargs):
        chat_id = kwargs.pop("chat_id", self.GROUP)
        is_private = kwargs.pop("is_private", False)
        sender_id = kwargs.pop("sender_id", 1)
        from_user = kwargs.pop("from_user", None)
        return _FakeHandlerEvent(
            _FakeMessage(text, from_user=from_user, **kwargs),
            chat_id=chat_id,
            sender_id=sender_id,
            is_private=is_private,
        )

    def _prompt(self):
        return self.stream_calls[0][1]["messages"][-1]["content"]

    def _model(self):
        return self.stream_calls[0][1]["model"]

    def _prefix(self):
        return self.stream_calls[0][1]["prefix"]

    def test_owner_gets_full_tool_menu(self):
        self._run(self._group_event("@danybot привет", sender_id=self.OWNER))
        self.assertEqual(self.stream_calls[0][1]["tools"], tools_module.BOT_TOOLS)

    def test_stranger_gets_public_tool_menu(self):
        self._run(self._group_event("@danybot привет", sender_id=1))
        self.assertEqual(self.stream_calls[0][1]["tools"], tools_module.PUBLIC_TOOLS)

    def test_coder_chat_gets_coder_tool_menu(self):
        bot.coder_chats.add(self.GROUP)
        self._run(self._group_event("@danybot привет", sender_id=self.OWNER))
        self.assertEqual(self.stream_calls[0][1]["tools"], tools_module.CODER_TOOLS)

    def test_empty_message_ignored(self):
        self._run(self._group_event(""))
        self.assertEqual(self.stream_calls, [])

    def _inline_text(self, query="привет как дела", sender_id=1):
        self.addCleanup(bot.inline_seen.clear)
        result = bot.build_inline_results(query, sender_id)[0]
        return cast(Any, result.input_message_content).message_text

    def _inline_event(self, text, **kwargs):
        kwargs.setdefault("via_bot", _VIA_BOT)
        return self._group_event(text, **kwargs)

    def test_inline_message_answers_group(self):
        text = self._inline_text(sender_id=1)
        self._run(self._inline_event(text, sender_id=1))
        self.assertEqual(len(self.stream_calls), 1)
        self.assertTrue(self._prompt().endswith("привет как дела"))

    def test_inline_message_text_has_no_mark(self):
        text = self._inline_text(sender_id=1)
        self.assertNotIn("\u2063", text)
        self.assertEqual(text, "привет как дела")

    def test_inline_message_still_answers_after_edit(self):
        text = self._inline_text(sender_id=1)
        self._run(self._inline_event(f"{text}, подробнее", sender_id=1))
        self.assertEqual(len(self.stream_calls), 1)
        self.assertTrue(self._prompt().endswith("привет как дела, подробнее"))

    def test_inline_message_without_via_bot_is_ignored(self):
        text = self._inline_text(sender_id=1)
        self._run(self._group_event(text, sender_id=1))
        self.assertEqual(self.stream_calls, [])

    def test_inline_message_via_foreign_bot_is_ignored(self):
        text = self._inline_text(sender_id=1)
        self._run(
            self._inline_event(text, sender_id=1, via_bot=SimpleNamespace(id=999))
        )
        self.assertEqual(self.stream_calls, [])

    def test_inline_message_is_answered_once(self):
        text = self._inline_text(sender_id=1)
        event = self._inline_event(text, sender_id=1)
        self._run(event)
        self._run(event)
        self.assertEqual(len(self.stream_calls), 1)

    def test_inline_command_runs_prompt_reply(self):
        text = self._inline_text(query="/help", sender_id=self.OWNER)
        event = self._run(self._inline_event(text, sender_id=self.OWNER))
        self.assertEqual(self.stream_calls, [])
        self.assertIn("DanyBOT - команды", event.sent[0])

    def test_private_message_from_bot_is_ignored(self):
        event = self._group_event(
            "привет",
            chat_id=555,
            is_private=True,
            sender_id=99,
            from_user=_MACHINE,
        )
        self._run(event)
        self.assertEqual(self.stream_calls, [])
        self.assertEqual(event.sent, [])

    def test_group_message_from_bot_is_answered(self):
        event = self._group_event("@danybot привет", sender_id=99, from_user=_MACHINE)
        self._run(event)
        self.assertEqual(len(self.stream_calls), 1)

    def test_sender_is_bot_checks_event_message(self):
        event = self._group_event("x", from_user=None)
        self.assertFalse(asyncio.run(bot._sender_is_bot(event)))
        event = self._group_event("x", from_user=_MACHINE)
        self.assertTrue(asyncio.run(bot._sender_is_bot(event)))
        self.assertFalse(asyncio.run(bot._sender_is_bot(SimpleNamespace())))
        private = self._group_event("привет", chat_id=556, is_private=True, sender_id=7)
        self._run(private)
        self.assertEqual(len(self.stream_calls), 1)

    def test_sender_is_bot_works_on_real_aiogram_message(self):
        machine = _aiogram_message(chat_type="private", text="спам").model_copy(
            update={
                "from_user": User(id=8, is_bot=True, first_name="Bot"),
            }
        )
        human = _aiogram_message(chat_type="private", text="привет")
        self.assertTrue(asyncio.run(bot._sender_is_bot(bot.BotEvent(machine))))
        self.assertFalse(asyncio.run(bot._sender_is_bot(bot.BotEvent(human))))
        without_sender = machine.model_copy(update={"from_user": None})
        self.assertFalse(asyncio.run(bot._sender_is_bot(bot.BotEvent(without_sender))))

    def test_private_bot_message_is_skipped_on_real_aiogram_message(self):
        machine = _aiogram_message(chat_type="private", text="я бот").model_copy(
            update={"from_user": User(id=8, is_bot=True, first_name="Bot")}
        )
        event = bot.BotEvent(machine)
        sent = []

        async def spy(text, buttons=None):
            sent.append(text)
            return bot.SentMessage(machine)

        event.reply = spy
        asyncio.run(bot.handler(event))
        self.assertEqual(self.stream_calls, [])
        self.assertEqual(sent, [])

    def test_bot_stats_reports_chat_state(self):
        bot.chat_history[777] = deque([{"role": "user", "content": "a"}], maxlen=10)
        bot.model_overrides[777] = "custom-model"
        stats = bot._bot_stats(777)
        self.assertEqual(stats["context_messages"], 1)
        self.assertEqual(stats["model"], "custom-model")
        self.assertGreaterEqual(stats["uptime_seconds"], 0)
        self.assertEqual(bot._bot_stats(778)["model"], userbot.DANYAPI_MODEL)

    def test_strip_trigger_keeps_alias_in_bot(self):
        self.assertEqual(bot._strip_trigger(".db привет", False), ".db привет")
        self.assertEqual(bot._strip_trigger("  .ai текст  ", False), ".ai текст")

    def test_bad_command_payload_is_ignored(self):
        def boom(_text):
            raise TypeError("bad command")

        saved = bot.handle_bot_commands
        bot.handle_bot_commands = boom
        self.addCleanup(setattr, bot, "handle_bot_commands", saved)
        self._run(self._group_event("@danybot привет"))
        self.assertEqual(len(self.stream_calls), 1)

    def test_owner_command_denied_for_stranger(self):
        event = self._run(self._group_event("/clear", sender_id=1))
        self.assertIn("только владельцу", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_model_command_saves_state(self):
        saved = bot.save_state
        calls = []
        bot.save_state = lambda: calls.append(1)
        self.addCleanup(setattr, bot, "save_state", saved)
        userbot.MODELS = ["m-one", "m-two"]
        event = self._run(self._group_event("/model m-one", sender_id=self.OWNER))
        self.assertIn("m-one", event.sent[0])
        self.assertEqual(calls, [1])

    def test_clear_command_saves_history(self):
        saved = bot.save_history
        calls = []
        bot.save_history = lambda: calls.append(1)
        self.addCleanup(setattr, bot, "save_history", saved)
        bot.chat_history[self.GROUP] = deque(
            [{"role": "user", "content": "x"}], maxlen=10
        )
        self._run(self._group_event("/clear", sender_id=self.OWNER))
        self.assertEqual(calls, [1])
        self.assertEqual(len(bot.chat_history[self.GROUP]), 0)

    def test_progress_callback_records_rounds_and_tools(self):
        bot.chat_history[self.GROUP] = deque(maxlen=10)
        self._run(self._group_event("@danybot привет", sender_id=self.OWNER))
        record = bot.TASKS.get(self.GROUP) or {}
        self.assertEqual(record.get("rounds"), 0)
        self.assertEqual(record.get("tools"), 0)
        self.assertEqual(record.get("status"), core.TASK_DONE)

    def test_progress_callback_records_counters(self):
        seen: dict = {}

        async def stream(*_args, **kwargs):
            progress = kwargs.get("progress_fn")
            if progress is not None:
                progress(3, 2, "остановлен по воле модели")
            return "ответ"

        saved = core.stream_answer
        core.stream_answer = stream
        self.addCleanup(setattr, core, "stream_answer", saved)
        self._run(self._group_event("@danybot привет", sender_id=self.OWNER))
        record = bot.TASKS.get(self.GROUP) or {}
        seen["status"] = record.get("status")
        self.assertEqual(record.get("rounds"), 3)
        self.assertEqual(record.get("tools"), 2)
        self.assertEqual(record.get("reason"), "остановлен по воле модели")
        self.assertEqual(seen["status"], core.TASK_STOPPED)

    def test_proxy_candidates_report_failure(self):
        async def boom(limit=40, deadline=180.0):
            raise RuntimeError("proxy pool down")

        saved = proxies.get_proxy_candidates
        self.addCleanup(setattr, proxies, "get_proxy_candidates", saved)
        proxies.get_proxy_candidates = boom
        self.assertEqual(asyncio.run(bot._proxy_candidates()), [])

    def test_proxy_candidates_return_values(self):
        async def ok(limit=40, deadline=180.0):
            return [{"proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080}]

        saved = proxies.get_proxy_candidates
        self.addCleanup(setattr, proxies, "get_proxy_candidates", saved)
        proxies.get_proxy_candidates = ok
        self.assertEqual(
            asyncio.run(bot._proxy_candidates()),
            [{"proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080}],
        )

    def test_start_bot_falls_back_to_direct_without_proxies(self):
        userbot.BOT_TOKEN = BOT_TOKEN_VALUE
        client = _FakeAiogramClient(username="danybot_bot", uid=4)
        seen: dict = {}

        async def no_proxies():
            return []

        for attr, value in (
            ("_proxy_candidates", no_proxies),
            ("_connect", mock.Mock(side_effect=lambda p, dc=None, address="": client)),
            ("bot_client", None),
            ("bot_username", ""),
            ("bot_id", 0),
        ):
            saved = getattr(bot, attr)
            self.addCleanup(setattr, bot, attr, saved)
            setattr(bot, attr, value)

        async def run():
            await bot.start_bot()
            seen["id"] = bot.bot_id

        with mock.patch.object(
            core,
            "dc_candidates",
            lambda *a, **k: [{"dc": 2, "address": core.DC_FALLBACK}],
        ):
            asyncio.run(run())
        self.assertEqual(seen["id"], 4)
        self.assertEqual(client.polled, 1)

    def test_reply_failure_is_logged(self):
        class Boom(_FakeHandlerEvent):
            async def reply(self, text, buttons=None):
                raise RPCError(request=None, message="telegram down")

        event = Boom(
            _FakeMessage("/settings"),
            chat_id=self.GROUP,
            sender_id=self.OWNER,
            is_private=False,
        )
        asyncio.run(bot.handler(event))
        self.assertEqual(event.sent, [])

    def test_generation_error_reports_to_chat(self):
        async def boom(*_args, **_kwargs):
            raise OSError("api down")

        saved = core.stream_answer
        core.stream_answer = boom
        self.addCleanup(setattr, core, "stream_answer", saved)
        event = self._run(self._group_event("@danybot привет"))
        self.assertIn("Ошибка", event.sent[0])

    def test_cancellation_of_own_task_is_reraised(self):
        async def boom(*_args, **_kwargs):
            raise asyncio.CancelledError

        saved = core.stream_answer
        core.stream_answer = boom
        self.addCleanup(setattr, core, "stream_answer", saved)

        async def scenario():
            task = asyncio.ensure_future(
                bot.handler(self._group_event("@danybot привет", sender_id=self.OWNER))
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_resolve_picked_model_reports_unknown(self):
        self.assertIsNone(bot._resolve_picked_model("#000000000000"))
        self.assertIsNone(bot._resolve_picked_model("нет-такой"))
        userbot.MODELS = ["m-one"]
        self.assertEqual(bot._resolve_picked_model("m-one"), "m-one")
        digest = bot._model_digest("m-one")
        self.assertEqual(bot._resolve_picked_model(f"#{digest}"), "m-one")

    def test_mention_helpers_without_username(self):
        saved = bot.bot_username
        bot.bot_username = ""
        self.addCleanup(setattr, bot, "bot_username", saved)
        self.assertIsNone(bot._mention_re())
        self.assertFalse(bot._is_mentioned("@danybot привет"))
        self.assertEqual(bot._strip_mention("@danybot привет"), "@danybot привет")

    def test_message_without_text_ignored(self):
        event = self._group_event("привет")
        event.message = None
        self._run(event)
        self.assertEqual(self.stream_calls, [])

    def test_missing_chat_id_ignored(self):
        event = self._group_event("привет")
        event.chat_id = None
        self._run(event)
        self.assertEqual(self.stream_calls, [])

    def test_group_message_needs_trigger(self):
        self._run(self._group_event("просто текст"))
        self.assertEqual(self.stream_calls, [])

    def test_db_trigger_never_active(self):
        self._run(self._group_event(".db привет"))
        self.assertEqual(self.stream_calls, [])
        userbot.ENABLE_USERBOT = True
        self._run(self._group_event(".db привет", msg_id=91))
        self.assertEqual(self.stream_calls, [])

    def test_mention_triggers(self):
        saved_username = bot.bot_username
        self.addCleanup(setattr, bot, "bot_username", saved_username)
        bot.bot_username = "danybot"
        self._run(self._group_event("@danybot привет"))
        self.assertEqual(len(self.stream_calls), 1)

    def test_reply_to_bot_triggers(self):
        replied = _FakeMessage("вопрос", msg_id=5, out=True)
        self._run(self._group_event("ответ", is_reply=True, reply_msg=replied))
        self.assertEqual(len(self.stream_calls), 1)
        self.assertIn("вопрос", self._prompt())

    def test_reply_without_text_uses_replied_text(self):
        replied = _FakeMessage("только реплай", msg_id=6, out=True)
        self._run(self._group_event("   ", is_reply=True, reply_msg=replied))
        self.assertIn("только реплай", self._prompt())

    def test_private_message_triggers(self):
        event = _FakeHandlerEvent(
            _FakeMessage("привет"), chat_id=7, sender_id=9, is_private=True
        )
        self._run(event)
        self.assertEqual(len(self.stream_calls), 1)

    def test_own_message_in_group_stays_quiet(self):
        self._run(self._group_event("моё сообщение", out=True))
        self.assertEqual(self.stream_calls, [])

    def test_own_mentioned_message_keeps_prefix(self):
        saved_username = bot.bot_username
        self.addCleanup(setattr, bot, "bot_username", saved_username)
        bot.bot_username = "danybot"
        self._run(self._group_event("@danybot моё сообщение", out=True, msg_id=21))
        self.assertEqual(self._prefix(), "@danybot моё сообщение\n\n")
        self.assertEqual(self.stream_calls[0][1]["self_edit_id"], 21)

    def test_prompt_is_truncated(self):
        saved_limit = userbot.MAX_REQUEST_LEN
        self.addCleanup(setattr, userbot, "MAX_REQUEST_LEN", saved_limit)
        userbot.MAX_REQUEST_LEN = 40
        self._run(self._group_event("@danybot " + "я" * 100 + "ХВОСТ"))
        self.assertNotIn("ХВОСТ", self._prompt())

    def test_mention_only_prompt_returns_early(self):
        saved_username = bot.bot_username
        self.addCleanup(setattr, bot, "bot_username", saved_username)
        bot.bot_username = "danybot"
        self._run(self._group_event("@danybot"))
        self.assertEqual(self.stream_calls, [])

    def test_cooldown_blocks_second_message(self):
        userbot.COOLDOWN = 60
        event1 = self._group_event("@danybot первый", msg_id=10)
        self._run(event1)
        event2 = self._group_event("@danybot второй", msg_id=11)
        self._run(event2)
        self.assertEqual(len(self.stream_calls), 1)

    def test_duplicate_message_id_skipped(self):
        event = self._group_event("@danybot привет", msg_id=42)
        self._run(event)
        bot.recent_reply_ids.add((self.GROUP, 42))
        self._run(self._group_event("@danybot привет", msg_id=42))
        self.assertEqual(len(self.stream_calls), 1)

    def test_coder_mode_uses_coder_tools(self):
        bot.coder_chats.add(self.GROUP)
        self._run(self._group_event("@danybot привет", sender_id=self.OWNER))
        self.assertTrue(self.stream_calls)
        bot.coder_chats.clear()

    def test_model_override_used(self):
        bot.model_overrides[self.GROUP] = "custom-model"
        self._run(self._group_event("@danybot привет"))
        self.assertEqual(self._model(), "custom-model")

    def test_stream_error_reports_to_user(self):
        async def failing_stream(*args, **kwargs):
            raise userbot.OpenAIError("boom")

        saved = core.stream_answer
        self.addCleanup(setattr, core, "stream_answer", saved)
        core.stream_answer = failing_stream
        event = self._group_event("@danybot привет")
        self._run(event)
        self.assertIn("Ошибка", event.sent[0])

    def test_unexpected_error_still_closes_task_record(self):
        async def broken_stream(*args, **kwargs):
            raise ZeroDivisionError("не из HANDLER_ERRORS")

        saved = core.stream_answer
        self.addCleanup(setattr, core, "stream_answer", saved)
        core.stream_answer = broken_stream
        with self.assertRaises(ZeroDivisionError):
            self._run(
                self._group_event("@danybot привет", chat_id=571, sender_id=self.OWNER)
            )
        record = bot.TASKS.get(571) or {}
        self.assertEqual(record.get("status"), core.TASK_FAILED)
        self.assertEqual(record.get("reason"), "ZeroDivisionError")

    def test_history_saver_marked_dirty(self):
        self._run(self._group_event("@danybot привет"))
        self.assertGreaterEqual(self.saver.dirty, 1)

    def test_lower_authority_waits_for_owner_run(self):
        registry = core.SessionRegistry()
        order = []

        async def scenario():
            async def owner_run():
                await asyncio.sleep(0.05)
                order.append("owner")

            async def other_run():
                order.append("other")

            owner_task = registry.start(4, owner_run(), scope="owner")
            other_task = registry.start(4, other_run(), scope="other")
            await asyncio.gather(owner_task, other_task)
            self.assertEqual(registry.running_chats(), 0)

        asyncio.run(scenario())
        self.assertEqual(order, ["owner", "other"])

    def test_owner_run_supersedes_other_run(self):
        registry = core.SessionRegistry()
        finished = []

        async def scenario():
            async def waiter(name):
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    finished.append(name)
                    raise

            async def quick():
                finished.append("quick")

            other_task = registry.start(4, waiter("other"), scope="other")
            await asyncio.sleep(0)
            owner_task = registry.start(4, quick(), scope="owner")
            await owner_task
            with self.assertRaises(asyncio.CancelledError):
                await other_task

        asyncio.run(scenario())
        self.assertEqual(sorted(finished), ["other", "quick"])

    def test_other_run_supersedes_other_run(self):
        registry = core.SessionRegistry()
        finished = []

        async def scenario():
            async def waiter(name):
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    finished.append(name)
                    raise

            async def quick():
                finished.append("quick")

            first = registry.start(4, waiter("first"), scope="other")
            await asyncio.sleep(0)
            second = registry.start(4, quick(), scope="other")
            await second
            with self.assertRaises(asyncio.CancelledError):
                await first

        asyncio.run(scenario())
        self.assertEqual(sorted(finished), ["first", "quick"])

    def test_queued_run_is_dropped_when_cancelled(self):
        registry = core.SessionRegistry()

        async def scenario():
            ran = []

            async def owner_run():
                await asyncio.sleep(5)

            async def other_run():
                ran.append(1)

            owner_task = registry.start(4, owner_run(), scope="owner")
            await asyncio.sleep(0)
            other_task = registry.start(4, other_run(), scope="other")
            await asyncio.sleep(0)
            other_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await other_task
            owner_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner_task
            self.assertEqual(ran, [])
            self.assertEqual(registry.running_chats(), 0)

        asyncio.run(scenario())

    def test_stranger_does_not_break_owner_request(self):
        self.addCleanup(bot.SESSIONS.cancel_all)
        started = asyncio.Event()
        finished = []
        calls = []

        async def stream(*_args, **_kwargs):
            calls.append(1)
            started.set()
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                finished.append("cancelled")
                raise
            finished.append("done")
            return "ответ"

        core.stream_answer = stream

        async def scenario():
            task = asyncio.ensure_future(
                bot.handler(
                    self._group_event(
                        "@danybot долгий", chat_id=66, sender_id=self.OWNER
                    )
                )
            )
            await started.wait()
            other = asyncio.ensure_future(
                bot.handler(
                    self._group_event("@danybot привет", chat_id=66, sender_id=1)
                )
            )
            await asyncio.gather(task, other)
            self.assertEqual(_task_outcome(task), "None")
            self.assertEqual(_task_outcome(other), "None")
            self.assertEqual(bot.SESSIONS.running_chats(), 0)

        asyncio.run(scenario())
        self.assertEqual(len(calls), 2)
        self.assertEqual(finished, ["done", "done"])

    def test_task_command_reports_journal(self):
        bot.TASKS.begin(55, "проверка журнала", model="m")
        bot.TASKS.finish(55, core.TASK_DONE)
        event = self._group_event("/task", chat_id=55, sender_id=self.OWNER)
        self._run(event)
        self.assertIn("Завершена", event.sent[0])
        self.assertIn("проверка журнала", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_task_command_without_history(self):
        event = self._group_event("/task", chat_id=56, sender_id=self.OWNER)
        self._run(event)
        self.assertIn("не было", event.sent[0])

    def test_task_command_is_owner_only(self):
        bot.TASKS.begin(58, "секретный промпт", model="m")
        event = self._group_event("/task", chat_id=58, sender_id=1)
        self._run(event)
        self.assertNotIn("секретный промпт", event.sent[0])
        self.assertIn("только владельцу", event.sent[0])

    def test_models_command_is_owner_only(self):
        event = self._group_event("/models", chat_id=59, sender_id=1)
        self._run(event)
        self.assertNotIn("Доступные модели", event.sent[0])
        self.assertIn("только владельцу", event.sent[0])

    def test_settings_show_task_state(self):
        event = self._run(self._group_event("/settings", sender_id=self.OWNER))
        self.assertIn("Задача / Task: нет", event.sent[0])
        bot.TASKS.begin(57, "задача", model="m")
        bot.TASKS.finish(57, core.TASK_STOPPED, reason="лимит", rounds=3, tools=2)
        text = bot._settings_text(57)
        self.assertIn("остановлена по лимиту", text)
        self.assertIn("3 раундов", text)

    def test_new_message_cancels_running_request(self):
        self.addCleanup(bot.SESSIONS.cancel_all)
        started = asyncio.Event()
        calls = []
        finished = []

        async def stream(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                await asyncio.sleep(5)
                finished.append(1)
            return "быстрый ответ"

        core.stream_answer = stream

        async def scenario():
            task = asyncio.ensure_future(
                bot.handler(
                    self._group_event(
                        "@danybot долгий", chat_id=77, sender_id=self.OWNER
                    )
                )
            )
            await started.wait()
            await bot.handler(
                self._group_event("@danybot новый", chat_id=77, sender_id=self.OWNER)
            )
            await _wait_done(task)
            self.assertEqual(_task_outcome(task), "None")
            self.assertEqual(len(calls), 2)
            self.assertEqual(bot.SESSIONS.running_chats(), 0)

        asyncio.run(scenario())
        self.assertEqual(finished, [])

    def test_coder_off_cancels_running_request(self):
        self.addCleanup(bot.SESSIONS.cancel_all)
        self.addCleanup(bot.coder_chats.clear)
        bot.coder_chats.add(77)
        started = asyncio.Event()
        finished = []

        async def stream(*args, **kwargs):
            started.set()
            await asyncio.sleep(5)
            finished.append(1)
            return "долгий ответ"

        core.stream_answer = stream

        async def scenario():
            task = asyncio.ensure_future(
                bot.handler(
                    self._group_event(
                        "@danybot долгий", chat_id=77, sender_id=self.OWNER
                    )
                )
            )
            await started.wait()
            await bot.handler(
                self._group_event("/coder off", chat_id=77, sender_id=self.OWNER)
            )
            await _wait_done(task)
            self.assertEqual(_task_outcome(task), "None")
            self.assertNotIn(77, bot.coder_chats)
            self.assertEqual(bot.SESSIONS.running_chats(), 0)

        asyncio.run(scenario())
        self.assertEqual(finished, [])

    def test_coder_toggle_button_cancels_running_request(self):
        self.addCleanup(bot.SESSIONS.cancel_all)
        self.addCleanup(bot.coder_chats.clear)
        bot.coder_chats.add(88)
        started = asyncio.Event()
        finished = []

        async def stream(*args, **kwargs):
            started.set()
            await asyncio.sleep(5)
            finished.append(1)
            return "долгий ответ"

        core.stream_answer = stream

        async def scenario():
            task = asyncio.ensure_future(
                bot.handler(
                    self._group_event(
                        "@danybot долгий", chat_id=88, sender_id=self.OWNER
                    )
                )
            )
            await started.wait()
            await bot.callback_handler(_CallbackEvent("settings:coder", 88, self.OWNER))
            await _wait_done(task)
            self.assertEqual(_task_outcome(task), "None")
            self.assertNotIn(88, bot.coder_chats)
            self.assertEqual(bot.SESSIONS.running_chats(), 0)

        asyncio.run(scenario())
        self.assertEqual(finished, [])

    def test_help_command_for_owner(self):
        event = self._group_event("/help", sender_id=self.OWNER)
        self._run(event)
        self.assertIn("/settings", event.sent[0])

    def test_help_command_works_for_everyone(self):
        event = self._group_event("/help", sender_id=99)
        self._run(event)
        self.assertIn("/settings", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_settings_command_for_owner(self):
        event = self._group_event("/settings", sender_id=self.OWNER)
        self._run(event)
        self.assertEqual(self.stream_calls, [])

    def test_prompt_command_for_owner(self):
        event = self._group_event("/prompt", sender_id=self.OWNER)
        self._run(event)
        self.assertEqual(
            event.sent[0], userbot.system_prompt_report(self.GROUP, mode="bot")
        )

    def test_prompt_command_ignored_without_owner(self):
        event = self._group_event("/prompt", sender_id=99)
        self._run(event)
        self.assertIn("владельцу", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_coder_command_requires_owner(self):
        event = self._group_event("/coder on", sender_id=99)
        self._run(event)
        self.assertIn("владельцу", event.sent[0])
        self.assertNotIn(self.GROUP, bot.coder_chats)

    def test_coder_command_toggles(self):
        event = self._group_event("/coder on", sender_id=self.OWNER)
        self._run(event)
        self.assertIn(self.GROUP, bot.coder_chats)
        self.assertIn("ВКЛ", event.sent[0])
        event = self._group_event("/coder status", sender_id=self.OWNER)
        self._run(event)
        self.assertIn("ON", event.sent[0])
        event = self._group_event("/coder off", sender_id=self.OWNER)
        self._run(event)
        self.assertNotIn(self.GROUP, bot.coder_chats)
        self.assertIn("ВЫКЛ", event.sent[0])

    def test_settings_command_ignored_without_owner(self):
        event = self._group_event("/settings", sender_id=99)
        self._run(event)
        self.assertIn("владельцу", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_visibility_command_for_owner(self):
        event = self._group_event("/reasoning off", sender_id=self.OWNER)
        self._run(event)
        self.assertIn(self.GROUP, bot.reasoning_hidden)
        self.assertEqual(self.stream_calls, [])

    def test_visibility_command_ignored_without_owner(self):
        event = self._group_event("/reasoning off", sender_id=99)
        self._run(event)
        self.assertIn("владельцу", event.sent[0])
        self.assertNotIn(self.GROUP, bot.reasoning_hidden)


class VisibilityCommandsTest(BotTestCase):
    CHAT_ID = 7591254790

    def test_aliases_parse(self):
        self.assertEqual(
            userbot.handle_commands(".db reasoning off"), ("reasoning", False)
        )
        self.assertEqual(userbot.handle_commands(".db tools on"), ("tools", True))
        self.assertEqual(
            userbot.handle_commands(".ai reasoning on"), ("reasoning", True)
        )
        self.assertEqual(userbot.handle_commands(".db tools"), ("tools_status", None))
        self.assertEqual(bot.handle_bot_commands("/reasoning on"), ("reasoning", True))
        self.assertEqual(bot.handle_bot_commands("/tools off"), ("tools", False))
        self.assertEqual(bot.handle_bot_commands("/tools"), ("tools_status", None))

    def test_apply_visibility_toggle(self):
        reasoning_hidden = set()
        tools_hidden = set()
        text_out, changed = core.apply_visibility_command(
            ("reasoning", False), self.CHAT_ID, reasoning_hidden, tools_hidden
        )
        self.assertTrue(changed)
        self.assertIn(self.CHAT_ID, reasoning_hidden)
        self.assertIn("скрыты", text_out)
        text_out, changed = core.apply_visibility_command(
            ("reasoning", True), self.CHAT_ID, reasoning_hidden, tools_hidden
        )
        self.assertTrue(changed)
        self.assertNotIn(self.CHAT_ID, reasoning_hidden)
        self.assertIn("показаны", text_out)

    def test_apply_visibility_status_does_not_change(self):
        reasoning_hidden = {self.CHAT_ID}
        tools_hidden = set()
        text_out, changed = core.apply_visibility_command(
            ("reasoning_status", None), self.CHAT_ID, reasoning_hidden, tools_hidden
        )
        self.assertFalse(changed)
        self.assertIn("скрыты", text_out)
        text_out, changed = core.apply_visibility_command(
            ("tools_status", None), self.CHAT_ID, reasoning_hidden, tools_hidden
        )
        self.assertFalse(changed)
        self.assertIn("показаны", text_out)

    def test_make_render_hides_sections(self):
        def inner(prefix, reasoning, tools, answer, show_reasoning, show_tools):
            reason = "".join(reasoning) if show_reasoning else "-"
            calls = ",".join(tools) if show_tools else "-"
            return f"{prefix}|{reason}|{calls}|{answer}"

        render = core.make_render(inner, {self.CHAT_ID}, set(), self.CHAT_ID)
        self.assertEqual(render("p", ["r"], ["t"], "a"), "p|-|t|a")
        render = core.make_render(inner, set(), {self.CHAT_ID}, self.CHAT_ID)
        self.assertEqual(render("p", ["r"], ["t"], "a"), "p|r|-|a")

    def test_visibility_persisted_in_state(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_vis_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        state_file = tmp / "state_userbot.json"
        core.save_state_file(
            state_file,
            {
                "model_overrides": {},
                "coder_chats": {1},
                "reasoning_hidden": {2},
                "tools_hidden": {3},
            },
        )
        parsed = core.load_state_file(state_file)
        if parsed is None:
            self.fail("state file did not parse")
        self.assertEqual(parsed["coder_chats"], {1})
        self.assertEqual(parsed["reasoning_hidden"], {2})
        self.assertEqual(parsed["tools_hidden"], {3})


class CoderModeTest(BotTestCase):
    CHAT_ID = 7591254790

    def setUp(self):
        super().setUp()
        tmp = Path(tempfile.mkdtemp(prefix="danybot_coder_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = tools_module.CODER_ROOT
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved)
        tools_module.CODER_ROOT = tmp
        (tmp / "DanyBOT").mkdir(parents=True, exist_ok=True)

    def test_aliases_parse(self):
        self.assertEqual(userbot.handle_commands(".db coder on"), ("coder", True))
        self.assertEqual(userbot.handle_commands(".ai coder off"), ("coder", False))
        self.assertEqual(userbot.handle_commands(".db coder"), ("coder_status", None))
        self.assertEqual(bot.handle_bot_commands("/coder off"), ("coder", False))
        self.assertEqual(bot.handle_bot_commands("/coder"), ("coder_status", None))

    def test_subagents_get_no_coder_tools(self):
        names = {t["function"]["name"] for t in subagents._select_tools(None)}
        self.assertNotIn("run_subagent", names)
        self.assertFalse(names & set(tools_module.FILE_TOOL_NAMES))

    def test_coder_flag_lives_in_bot(self):
        saved = set(bot.coder_chats)
        bot.coder_chats.clear()
        self.addCleanup(bot.coder_chats.clear)
        self.addCleanup(bot.coder_chats.update, saved)
        self.assertFalse(bot.is_coder(self.CHAT_ID))
        bot.coder_chats.add(self.CHAT_ID)
        self.assertTrue(bot.is_coder(self.CHAT_ID))

    def test_coder_active_requires_owner(self):
        saved = set(bot.coder_chats)
        saved_owners = userbot.OWNER_IDS
        bot.coder_chats.clear()
        userbot.OWNER_IDS = {self.CHAT_ID}
        self.addCleanup(bot.coder_chats.clear)
        self.addCleanup(bot.coder_chats.update, saved)
        self.addCleanup(setattr, userbot, "OWNER_IDS", saved_owners)
        bot.coder_chats.add(self.CHAT_ID)
        self.assertTrue(bot.coder_active_for(self.CHAT_ID, self.CHAT_ID))
        self.assertFalse(bot.coder_active_for(self.CHAT_ID + 1, self.CHAT_ID))

    def test_userbot_handler_does_not_switch_to_coder(self):
        source = (PROJECT_DIR / "userbot.py").read_text(encoding="utf-8")
        self.assertNotIn("CODER_TOOLS if", source)
        self.assertNotIn('mode="coder"', source)

    def test_menu_covers_every_help_command(self):
        source = (PROJECT_DIR / "bot.py").read_text(encoding="utf-8")
        menu = set(menu_commands())
        help_block = source[source.index("BOT_HELP_TEXT = (") :]
        help_block = help_block[: help_block.index("\n)\n")]
        for name in menu:
            self.assertIn(f"/{name}", help_block)
        for name in ("settings",):
            self.assertIn(name, menu)
        for name in ("task",):
            self.assertIn(name, menu)

    def test_bot_handler_uses_coder_tools(self):
        source = (PROJECT_DIR / "bot.py").read_text(encoding="utf-8")
        self.assertIn("tool_menu = tools_module.CODER_TOOLS", source)
        self.assertIn("tool_menu = tools_module.BOT_TOOLS", source)
        self.assertIn("tool_menu = tools_module.PUBLIC_TOOLS", source)
        self.assertIn('mode = "coder" if coder_active else "bot"', source)

    def test_db_trigger_never_active(self):
        self.assertFalse(bot.NO_TEXT_TRIGGER)
        for text in (".db привет", ".ai привет", "привет .ai", "привет"):
            with self.subTest(text=text):
                self.assertEqual(bot._strip_trigger(text, bot.NO_TEXT_TRIGGER), text)

    def test_paths_are_confined_to_root(self):
        inside, err = tools_module._resolve_path("DanyBOT/tools.py")
        self.assertEqual(err, "")
        self.assertTrue(str(inside).startswith(str(tools_module.CODER_ROOT)))
        outside, err2 = tools_module._resolve_path("/etc/passwd")
        self.assertIsNone(outside)
        self.assertIn("вне разрешённого корня", err2)
        escaped, err3 = tools_module._resolve_path("../etc/passwd")
        self.assertIsNone(escaped)
        self.assertIn("вне разрешённого корня", err3)

    def test_file_tools_roundtrip(self):
        tmp = tools_module.CODER_ROOT / ".coder_selftest"
        self.addCleanup(shutil.rmtree, tmp, True)
        target = ".coder_selftest/note.txt"
        written = _owner_tool(
            "write_file", {"path": target, "content": "alpha\nbeta\n"}, -100
        )
        self.assertIn("Создан", written)
        read = _owner_tool("read_file", {"path": target}, -100)
        self.assertIn("1|alpha", read)
        edited = _owner_tool(
            "edit_file",
            {"path": target, "old_string": "beta", "new_string": "gamma"},
            -100,
        )
        self.assertIn("Изменён", edited)
        found = _owner_tool(
            "search_files", {"pattern": "gamma", "path": ".coder_selftest"}, -100
        )
        self.assertIn("note.txt:2", found)
        listed = _owner_tool("list_dir", {"path": ".coder_selftest"}, -100)
        self.assertIn("note.txt", listed)
        missing = _owner_tool("read_file", {"path": "/etc/passwd"}, -100)
        self.assertIn("вне разрешённого корня", missing)

    def test_execute_script_runs_and_returns_output(self):
        result = _owner_tool(
            "execute_script", {"code": "print('alpha')\nprint(1 + 2)"}, -100
        )
        self.assertIn("rc=0", result)
        self.assertIn("alpha", result)
        self.assertIn("3", result)

    def test_execute_script_reports_traceback(self):
        result = _owner_tool(
            "execute_script", {"code": "raise ValueError('boom')"}, -100
        )
        self.assertIn("rc=1", result)
        self.assertIn("ValueError", result)

    def test_execute_script_empty_code(self):
        self.assertEqual(
            _owner_tool("execute_script", {}, -100),
            "Пустой код.",
        )

    def test_execute_script_cwd_outside_root(self):
        result = _owner_tool(
            "execute_script", {"code": "print(1)", "cwd": "/etc"}, -100
        )
        self.assertIn("вне разрешённого корня", result)

    def test_execute_script_cwd_inside_root(self):
        result = _owner_tool(
            "execute_script",
            {"code": "import os\nprint(os.getcwd())", "cwd": "DanyBOT"},
            -100,
        )
        self.assertIn("DanyBOT", result)

    def test_coder_prompt_mentions_every_tool(self):
        names = {t["function"]["name"] for t in tools_module.CODER_TOOLS}
        prompt = userbot.CODER_SYSTEM_PROMPT
        for name in sorted(names):
            with self.subTest(tool=name):
                self.assertIn(name, prompt)

    def test_coder_prompt_has_all_coder_tools(self):
        names = {t["function"]["name"] for t in tools_module.CODER_TOOLS}
        self.assertIn("execute_script", names)
        self.assertIn("execute_script", userbot.CODER_SYSTEM_PROMPT)


class _CallbackEvent:
    def __init__(self, data, chat_id, sender_id, is_private=None):
        self.data = data
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_private = (
            bool(chat_id) and chat_id > 0 if is_private is None else is_private
        )
        self.answers = []
        self.edits = []
        self.replies = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def edit(self, text, buttons=None):
        self.edits.append((text, buttons))

    async def reply(self, text, buttons=None):
        self.replies.append((text, buttons))
        return SimpleNamespace(id=1)


class SettingsMenuTest(BotTestCase):
    CHAT_ID = 7591254790

    def setUp(self):
        super().setUp()
        tmp = Path(tempfile.mkdtemp(prefix="danybot_settings_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        saved_state = bot.STATE_FILE
        saved_history = bot.HISTORY_FILE
        bot.STATE_FILE = tmp / "state_bot.json"
        bot.HISTORY_FILE = tmp / "history_bot.json"
        self.addCleanup(setattr, bot, "STATE_FILE", saved_state)
        self.addCleanup(setattr, bot, "HISTORY_FILE", saved_history)
        saved_owners = userbot.OWNER_IDS
        userbot.OWNER_IDS = {self.CHAT_ID}
        self.addCleanup(setattr, userbot, "OWNER_IDS", saved_owners)
        saved = {
            "coder": set(bot.coder_chats),
            "reasoning": set(bot.reasoning_hidden),
            "tools": set(bot.tools_hidden),
            "models": dict(bot.model_overrides),
        }

        def restore():
            bot.coder_chats.clear()
            bot.coder_chats.update(saved["coder"])
            bot.reasoning_hidden.clear()
            bot.reasoning_hidden.update(saved["reasoning"])
            bot.tools_hidden.clear()
            bot.tools_hidden.update(saved["tools"])
            bot.model_overrides.clear()
            bot.model_overrides.update(saved["models"])

        self.addCleanup(restore)

    def test_settings_aliases_parse(self):
        self.assertEqual(bot.handle_bot_commands("/settings"), ("settings", None))

    def test_ping_and_history_commands_gone(self):
        for text in ("/ping", "/history"):
            with self.subTest(text=text):
                self.assertIsNone(bot.handle_bot_commands(text))
        for text in (".db ping", ".db history"):
            with self.subTest(text=text):
                self.assertIsNone(userbot.handle_commands(text))

    def test_settings_rows_expose_every_toggle(self):
        rows = bot._settings_rows(self.CHAT_ID)
        self.assertEqual(
            rows_data(rows),
            {
                "settings:model",
                "settings:reasoning",
                "settings:tools",
                "settings:coder",
                "settings:inline",
                "settings:clear",
                "settings:prompt",
            },
        )

    def test_callback_toggles_inline_mode(self):
        saved = bot.inline_mode
        self.addCleanup(setattr, bot, "inline_mode", saved)
        bot.inline_mode = True
        event = _CallbackEvent("settings:inline", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertFalse(bot.inline_mode)
        self.assertIn("Инлайн-режим / Inline: выкл", bot._settings_text(self.CHAT_ID))
        asyncio.run(bot.callback_handler(event))
        self.assertTrue(bot.inline_mode)

    def test_inline_mode_survives_restart(self):
        saved = bot.inline_mode
        self.addCleanup(setattr, bot, "inline_mode", saved)
        bot.inline_mode = False
        bot.save_state()
        bot.inline_mode = True
        bot.load_state()
        self.assertFalse(bot.inline_mode)

    def test_settings_rows_report_inline_state(self):
        saved = bot.inline_mode
        self.addCleanup(setattr, bot, "inline_mode", saved)
        bot.inline_mode = False
        labels = [
            btn.text for btn in rows_data_buttons(bot._settings_rows(self.CHAT_ID))
        ]
        self.assertIn("Инлайн-режим: выкл", labels)
        bot.inline_mode = True
        labels = [
            btn.text for btn in rows_data_buttons(bot._settings_rows(self.CHAT_ID))
        ]
        self.assertIn("Инлайн-режим: вкл", labels)

    def test_settings_text_reports_state(self):
        text = bot._settings_text(self.CHAT_ID)
        self.assertIn("Настройки / Settings", text)
        self.assertIn(userbot.DANYAPI_MODEL, text)
        self.assertIn("Контекст / Context", text)

    def test_callback_toggles_visibility(self):
        bot.reasoning_hidden.discard(self.CHAT_ID)
        bot.tools_hidden.discard(self.CHAT_ID)
        event = _CallbackEvent("settings:reasoning", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn(self.CHAT_ID, bot.reasoning_hidden)
        event = _CallbackEvent("settings:tools", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn(self.CHAT_ID, bot.tools_hidden)

    def test_callback_toggles_coder(self):
        bot.coder_chats.discard(self.CHAT_ID)
        event = _CallbackEvent("settings:coder", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertTrue(bot.is_coder(self.CHAT_ID))

    def test_callback_rejects_non_owner(self):
        event = _CallbackEvent("settings:reasoning", self.CHAT_ID, self.CHAT_ID + 1)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(event.edits, [])
        self.assertTrue(event.answers[-1][1])

    def test_callback_clear_resets_context(self):
        self.addCleanup(bot.chat_history.pop, self.CHAT_ID, None)
        bot.chat_history[self.CHAT_ID] = deque(
            [{"role": "user", "content": "x"}], maxlen=userbot.DM_HISTORY_LIMIT
        )
        event = _CallbackEvent("settings:clear", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(len(bot.chat_history[self.CHAT_ID]), 0)

    def test_callback_model_picker(self):
        userbot.MODELS = ["m-one", "m-two"]
        event = _CallbackEvent("settings:model", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        picked = rows_data(event.edits[-1][1])
        self.assertIn("settings:pick:m-one", picked)
        self.assertIn("settings:pick:m-two", picked)
        self.assertIn("settings:main", picked)
        pick = _CallbackEvent("settings:pick:m-two", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(pick))
        self.assertEqual(bot.model_overrides[self.CHAT_ID], "m-two")

    def test_callback_unknown_action(self):
        event = _CallbackEvent("settings:nope", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(event.edits, [])
        self.assertEqual(event.answers[-1][1], True)

    def test_callback_without_chat_id_is_ignored(self):
        event = _CallbackEvent("settings:reasoning", None, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(event.answers, [])
        self.assertEqual(event.edits, [])

    def test_callback_decodes_bytes_payload(self):
        event = _CallbackEvent(b"settings:reasoning", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn(self.CHAT_ID, bot.reasoning_hidden)
        self.assertEqual(event.edits[-1][0].splitlines()[0], "Настройки / Settings")

    def test_callback_pick_reports_unavailable_model(self):
        event = _CallbackEvent("settings:pick:нет-такой", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(event.answers[0][0], "Модель больше недоступна.")
        self.assertEqual(event.answers[0][1], True)
        self.assertIn("settings:model", rows_data(event.edits[-1][1]))

    def test_callback_models_lists_all(self):
        userbot.MODELS = ["m-one", "m-two"]
        event = _CallbackEvent("settings:models", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        text = "\n".join(reply[0] for reply in event.replies)
        self.assertIn("m-one", text)
        self.assertIn("m-two", text)

    def test_callback_prompt_reports_system_prompt(self):
        event = _CallbackEvent("settings:prompt", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn("Режим / Mode", "\n".join(reply[0] for reply in event.replies))

    def test_callback_prompt_uses_coder_mode(self):
        bot.coder_chats.add(self.CHAT_ID)
        self.addCleanup(bot.coder_chats.discard, self.CHAT_ID)
        event = _CallbackEvent("settings:prompt", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn("coder", "\n".join(reply[0] for reply in event.replies))

    def test_callback_back_to_main(self):
        event = _CallbackEvent("settings:main", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn("settings:model", rows_data(event.edits[-1][1]))

    def test_callback_edit_failure_is_tolerated(self):
        class Boom(_CallbackEvent):
            async def edit(self, text, buttons=None):
                raise RPCError(request=None, message="message is not modified")

        bot.reasoning_hidden.discard(self.CHAT_ID)
        event = Boom("settings:reasoning", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn(self.CHAT_ID, bot.reasoning_hidden)
        self.assertEqual(event.answers[-1][0], "Готово.")

    def test_callback_coder_off_cancels_running_request(self):
        bot.coder_chats.add(self.CHAT_ID)
        self.addCleanup(bot.coder_chats.discard, self.CHAT_ID)
        event = _CallbackEvent("settings:coder", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertNotIn(self.CHAT_ID, bot.coder_chats)
        event = _CallbackEvent("settings:coder", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn(self.CHAT_ID, bot.coder_chats)

    def test_callback_clear_uses_group_limit_in_group(self):
        group = -100777
        bot.chat_history[group] = deque(
            [{"role": "user", "content": "x"}], maxlen=userbot.DM_HISTORY_LIMIT
        )
        self.addCleanup(bot.chat_history.pop, group, None)
        event = _CallbackEvent("settings:clear", group, self.CHAT_ID, is_private=False)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(len(bot.chat_history[group]), 0)
        self.assertEqual(bot.chat_history[group].maxlen, userbot.GROUP_HISTORY_LIMIT)

    def test_settings_text_shows_dm_limit_in_private_chat(self):
        text = bot._settings_text(self.CHAT_ID, True)
        self.assertIn(f"/{userbot.DM_HISTORY_LIMIT}", text)

    def test_callback_pick_survives_long_model_name(self):
        long_name = "vendor/" + "x" * 70 + "/model"
        userbot.MODELS = [long_name]
        self.addCleanup(setattr, userbot, "MODELS", userbot.MODELS)
        data = bot._pick_data(long_name)
        self.assertLessEqual(len(data.encode("utf-8")), bot.CALLBACK_MAX_BYTES)
        self.assertEqual(bot._resolve_picked_model(data.split(":")[2]), long_name)
        event = _CallbackEvent(data, self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(bot.model_overrides[self.CHAT_ID], long_name)

    def test_settings_text_lists_all_states(self):
        bot.reasoning_hidden.add(self.CHAT_ID)
        bot.tools_hidden.add(self.CHAT_ID)
        bot.coder_chats.add(self.CHAT_ID)
        text = bot._settings_text(self.CHAT_ID)
        self.assertIn("Рассуждения", text)
        self.assertIn("Инструменты", text)
        self.assertIn("Кодер-режим", text)
        self.assertIn("вкл", text)

    def _coder_button_text(self):
        return next(
            btn.text
            for btn in rows_data_buttons(bot._settings_rows(self.CHAT_ID))
            if btn_data(btn) == "settings:coder"
        )

    def test_coder_button_shows_on_off(self):
        bot.coder_chats.discard(self.CHAT_ID)
        self.assertIn("выкл", self._coder_button_text())
        bot.coder_chats.add(self.CHAT_ID)
        self.assertIn("вкл", self._coder_button_text())

    def test_visibility_buttons_stay_visibility_wording(self):
        bot.reasoning_hidden.discard(self.CHAT_ID)
        text = next(
            btn.text
            for btn in rows_data_buttons(bot._settings_rows(self.CHAT_ID))
            if btn_data(btn) == "settings:reasoning"
        )
        self.assertIn("видно", text)


class InlineMarkTest(BotTestCase):
    def test_inline_helpers_are_gone(self):
        for name in (
            "INLINE_MARK",
            "INLINE_MARK_RE",
            "INLINE_MARK_WIDTH",
            "INLINE_TOKEN_LEN",
            "new_inline_token",
            "inline_marked_text",
            "inline_token_of",
            "strip_inline_mark",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(core, name))

    def test_command_matches_exact(self):
        self.assertEqual(
            core.inline_command_matches("/help", False), [("/help", "Справка / Help")]
        )
        self.assertEqual(
            core.inline_command_matches("  /Help@danybot_bot  ", False),
            [("/help", "Справка / Help")],
        )

    def test_command_matches_prefix_offers_each_variant(self):
        self.assertEqual(
            [cmd for cmd, _ in core.inline_command_matches("/mo", True)],
            ["/model", "/models"],
        )

    def test_command_matches_gate_owner_commands(self):
        self.assertEqual(core.inline_command_matches("/mo", False), [])
        self.assertEqual(core.inline_command_matches("/clear", False), [])
        self.assertEqual(
            [cmd for cmd, _ in core.inline_command_matches("/clear", True)], ["/clear"]
        )

    def test_command_matches_ignore_unknown(self):
        for text in ("привет", "/zzz", "/", "", "help", "/помоги"):
            with self.subTest(text=text):
                self.assertEqual(core.inline_command_matches(text, True), [])

    def test_command_matches_hide_status_command(self):
        self.assertEqual(
            [cmd for cmd, _ in core.inline_command_matches("/coder", True)], ["/coder"]
        )

    def test_state_round_trip_keeps_inline_mode(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_inline_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "state.json"
        base = {
            "model_overrides": {},
            "coder_chats": set(),
            "reasoning_hidden": set(),
            "tools_hidden": set(),
        }
        self.assertTrue(core.save_state_file(target, {**base, "inline_mode": False}))
        loaded = core.load_state_file(target)
        self.assertIsNotNone(loaded)
        self.assertFalse(cast(Any, loaded)["inline_mode"])
        self.assertTrue(core.save_state_file(target, {**base, "inline_mode": True}))
        self.assertTrue(cast(Any, core.load_state_file(target))["inline_mode"])

    def test_state_omits_missing_inline_mode(self):
        parsed = core.parse_state_data(
            {
                "model_overrides": {},
                "coder_chats": [],
                "reasoning_hidden": [],
                "tools_hidden": [],
            }
        )
        self.assertNotIn("inline_mode", parsed)

    def test_state_ignores_non_bool_inline_mode(self):
        parsed = core.parse_state_data({"inline_mode": "yes"})
        self.assertNotIn("inline_mode", parsed)

    def test_mode_store_hides_absent_inline_mode(self):
        store = core.ModeStore({"model_overrides": {}})
        self.assertIsNone(getattr(store, "inline_mode", None))


def inline_marked_text(results, index=0):
    content = cast(Any, results[index].input_message_content)
    return cast(str, content.message_text)


def inline_texts(results):
    return [inline_marked_text(results, index) for index in range(len(results))]


class InlineResultsTest(BotTestCase):
    OWNER = 5
    STRANGER = 6

    def setUp(self):
        super().setUp()
        saved = userbot.OWNER_IDS
        self.addCleanup(setattr, userbot, "OWNER_IDS", saved)
        userbot.OWNER_IDS = {self.OWNER}

    def test_ask_result_carries_query_without_mark(self):
        results = bot.build_inline_results("  привет как дела  ", self.STRANGER)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "ask")
        self.assertEqual(results[0].title, userbot.BOT_NAME)
        self.assertEqual(results[0].description, "привет как дела")
        self.assertEqual(inline_marked_text(results), "привет как дела")
        content = cast(Any, results[0].input_message_content)
        self.assertTrue(content.disable_web_page_preview)

    def test_empty_query_lists_owner_commands(self):
        results = bot.build_inline_results("", self.OWNER)
        self.assertEqual(
            [item.id for item in results],
            ["cmd:help", "cmd:models", "cmd:task", "cmd:clear"],
        )
        for text in inline_texts(results):
            self.assertNotIn("\u2063", text)

    def test_empty_query_hides_owner_commands_from_stranger(self):
        self.assertEqual(
            [item.id for item in bot.build_inline_results("", self.STRANGER)],
            ["cmd:help"],
        )

    def test_query_command_replaces_ask_result(self):
        results = bot.build_inline_results("/he", self.STRANGER)
        self.assertEqual([item.id for item in results], ["cmd:help"])
        self.assertEqual(inline_marked_text(results), "/help")

    def test_long_query_is_clipped(self):
        results = bot.build_inline_results("я" * 9000, self.STRANGER)
        marked = inline_marked_text(results)
        self.assertLessEqual(len(marked), bot.INLINE_TEXT_LIMIT)
        self.assertLessEqual(
            len(cast(str, results[0].description) or ""), bot.INLINE_DESC_LIMIT
        )
        self.assertLessEqual(len(results[0].title), bot.INLINE_TITLE_LIMIT)

    def test_result_ids_are_stable(self):
        first = bot.build_inline_results("привет", self.STRANGER)[0]
        second = bot.build_inline_results("привет", self.STRANGER)[0]
        self.assertEqual(first.id, second.id)
        self.assertEqual(inline_marked_text([first]), inline_marked_text([second]))

    def test_disabled_mode_answers_with_nothing(self):
        calls = []

        class _Client:
            async def answer_inline(self, query_id, results):
                calls.append((query_id, results))

        saved_client = bot.bot_client
        saved_mode = bot.inline_mode
        self.addCleanup(setattr, bot, "bot_client", saved_client)
        self.addCleanup(setattr, bot, "inline_mode", saved_mode)
        bot.bot_client = _Client()
        bot.inline_mode = False
        asyncio.run(
            bot.inline_handler(
                cast(Any, SimpleNamespace(inline_query_id="iq", query="x", sender_id=6))
            )
        )
        self.assertEqual(calls, [("iq", [])])

    def test_handler_answers_with_results(self):
        calls = []

        class _Client:
            async def answer_inline(self, query_id, results):
                calls.append((query_id, list(results)))

        saved_client = bot.bot_client
        saved_mode = bot.inline_mode
        self.addCleanup(setattr, bot, "bot_client", saved_client)
        self.addCleanup(setattr, bot, "inline_mode", saved_mode)
        bot.bot_client = _Client()
        bot.inline_mode = True
        asyncio.run(
            bot.inline_handler(
                cast(
                    Any,
                    SimpleNamespace(inline_query_id="iq", query="привет", sender_id=6),
                )
            )
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "iq")
        self.assertEqual([item.id for item in calls[0][1]], ["ask"])

    def test_handler_survives_client_error(self):
        class _Client:
            async def answer_inline(self, query_id, results):
                raise TelegramBadRequest(_NO_METHOD, "query is too old")

        saved_client = bot.bot_client
        saved_mode = bot.inline_mode
        self.addCleanup(setattr, bot, "bot_client", saved_client)
        self.addCleanup(setattr, bot, "inline_mode", saved_mode)
        bot.bot_client = _Client()
        bot.inline_mode = True
        with self.assertLogs("danybot.bot", level="WARNING"):
            asyncio.run(
                bot.inline_handler(
                    cast(
                        Any,
                        SimpleNamespace(
                            inline_query_id="iq", query="привет", sender_id=6
                        ),
                    )
                )
            )

    def test_inline_event_reads_fields(self):
        event = bot.BotInlineEvent(
            cast(
                Any,
                InlineQuery(
                    id="iq7",
                    from_user=User(id=8, is_bot=False, first_name="A"),
                    query="  привет  ",
                    offset="",
                ),
            )
        )
        self.assertEqual(event.inline_query_id, "iq7")
        self.assertEqual(event.query, "привет")
        self.assertEqual(event.sender_id, 8)

    def test_client_sends_payload_without_none(self):
        inner = _FakeAiogramBot()
        client = bot.BotClient(inner)
        results = bot.build_inline_results("привет", 6)
        asyncio.run(client.answer_inline("iq", results))
        self.assertEqual(len(inner.inline_calls), 1)
        call = inner.inline_calls[0]
        self.assertEqual(call["id"], "iq")
        self.assertEqual(call["cache_time"], bot.INLINE_CACHE_TIME)
        self.assertTrue(call["is_personal"])
        self.assertEqual([item.id for item in call["results"]], ["ask"])
        self.assertEqual(call["results"], results)

    def test_client_serializes_results_for_api(self):
        session = cast(Any, _local_bot()).session
        results = bot.build_inline_results("привет", 6)
        prepared = session.prepare_value(
            {
                "results": results,
                "inline_query_id": "iq",
                "cache_time": bot.INLINE_CACHE_TIME,
                "is_personal": True,
            },
            bot=cast(Any, _local_bot()),
            files={},
        )
        payload = json.loads(prepared)
        self.assertEqual(payload["results"][0]["type"], "article")
        self.assertNotIn("thumbnail", payload["results"][0])
        self.assertNotIn("\u2063", payload["results"][0])
        self.assertEqual(
            payload["results"][0]["input_message_content"]["message_text"],
            "привет",
        )

    def test_client_error_becomes_rpc(self):
        client = bot.BotClient(
            _FakeAiogramBot(inline_error=TelegramBadRequest(_NO_METHOD, "too old"))
        )
        with self.assertRaises(RPCError):
            asyncio.run(client.answer_inline("iq", []))


class InlineClaimTest(BotTestCase):
    def setUp(self):
        super().setUp()
        bot.inline_seen.clear()
        self.addCleanup(bot.inline_seen.clear)
        saved_bot_id = bot.bot_id
        self.addCleanup(setattr, bot, "bot_id", saved_bot_id)
        bot.bot_id = _BOT_SELF_ID

    def test_message_claim_is_single_use(self):
        self.assertTrue(bot._claim_inline(6, 10))
        self.assertFalse(bot._claim_inline(6, 10))

    def test_message_claim_is_per_chat(self):
        self.assertTrue(bot._claim_inline(6, 10))
        self.assertTrue(bot._claim_inline(7, 10))

    def test_message_claim_is_per_message(self):
        self.assertTrue(bot._claim_inline(6, 10))
        self.assertTrue(bot._claim_inline(6, 11))

    def test_expired_entries_are_dropped(self):
        self.assertTrue(bot._claim_inline(6, 10))
        past = time.monotonic() - 1
        for key in list(bot.inline_seen):
            bot.inline_seen[key] = past
        self.assertTrue(bot._claim_inline(6, 10))
        self.assertEqual(list(bot.inline_seen), [(6, 10)])

    def test_via_bot_detection_matches_own_bot(self):
        self.assertTrue(bot._is_via_own_bot(_FakeMessage("x", via_bot=_VIA_BOT)))

    def test_via_bot_detection_ignores_foreign_bot(self):
        self.assertFalse(
            bot._is_via_own_bot(_FakeMessage("x", via_bot=SimpleNamespace(id=999)))
        )

    def test_via_bot_detection_ignores_plain_message(self):
        self.assertFalse(bot._is_via_own_bot(_FakeMessage("x")))

    def test_via_bot_detection_requires_bot_id(self):
        saved = bot.bot_id
        self.addCleanup(setattr, bot, "bot_id", saved)
        bot.bot_id = 0
        self.assertFalse(bot._is_via_own_bot(_FakeMessage("x", via_bot=_VIA_BOT)))

    def test_seen_stores_stay_bounded(self):
        saved_max = bot.INLINE_SEEN_MAX
        self.addCleanup(setattr, bot, "INLINE_SEEN_MAX", saved_max)
        bot.INLINE_SEEN_MAX = 4
        for index in range(20):
            bot._claim_inline(6, index)
        self.assertLessEqual(len(bot.inline_seen), 5)

    def test_evict_oldest_handles_dicts_and_sets(self):
        mapping = {index: index for index in range(10)}
        bucket = set(range(10))
        core._evict_oldest(mapping, 6)
        core._evict_oldest(bucket, 6)
        self.assertEqual(len(mapping), 6)
        self.assertEqual(len(bucket), 6)
        self.assertEqual(sorted(mapping), list(range(4, 10)))

    def test_disconnect_drops_seen_inline(self):
        bot._claim_inline(6, 10)
        client = _FakeAiogramBot()
        saved_client = bot.bot_client
        self.addCleanup(setattr, bot, "bot_client", saved_client)
        bot.bot_client = bot.BotClient(client)
        asyncio.run(bot.disconnect_quietly())
        self.assertEqual(bot.inline_seen, {})


class UserbotHelpersTest(BotTestCase):
    def test_registry_helpers_are_gone(self):
        for name in ("register_sender", "is_unrestricted", "_restricted_chats"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(userbot, name))

    def test_model_for(self):
        saved = dict(userbot.model_overrides)
        userbot.model_overrides.clear()
        self.addCleanup(userbot.model_overrides.update, saved)
        self.assertEqual(userbot.model_for(1), userbot.DANYAPI_MODEL)
        userbot.model_overrides[1] = "custom"
        self.assertEqual(userbot.model_for(1), "custom")

    def test_system_for(self):
        self.assertTrue(userbot.system_for(1).startswith(userbot.SYSTEM_PROMPT))
        self.assertTrue(
            userbot.system_for(1, mode="bot").startswith(userbot.SYSTEM_PROMPT_BOT)
        )
        self.assertTrue(
            userbot.system_for(1, mode="coder").startswith(userbot.CODER_SYSTEM_PROMPT)
        )

    def test_make_session_plain(self):
        self.assertEqual(userbot.make_session("plain"), "plain")

    def test_get_sender_label(self):
        class _Event:
            sender_id = 5

            async def get_sender(self):
                return SimpleNamespace(first_name="A", last_name="B", username="u")

        self.assertEqual(asyncio.run(userbot.get_sender_label(_Event())), "A B (@u)")

    def test_get_sender_label_no_sender(self):
        class _Event:
            sender_id = 9

            async def get_sender(self):
                return None

        self.assertEqual(asyncio.run(userbot.get_sender_label(_Event())), "9")

    def test_sanitize_disabled(self):
        saved = userbot.SANITIZE_ENABLED
        userbot.SANITIZE_ENABLED = False
        self.addCleanup(setattr, userbot, "SANITIZE_ENABLED", saved)
        self.assertEqual(asyncio.run(userbot.sanitize_tool_output("x", "m")), "x")

    def test_sanitize_unrestricted(self):
        self.assertEqual(asyncio.run(userbot.sanitize_tool_output("x", "m", True)), "x")

    def test_sanitize_cleans_output(self):
        saved_ai = userbot.ai
        userbot.ai = NonStreamAI(NonStreamResponse(NonStreamMessage(content="clean")))
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        self.assertEqual(
            asyncio.run(userbot.sanitize_tool_output("secret", "m")), "clean"
        )

    def test_sanitize_error_hides(self):
        class _Boom:
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kwargs):
                        raise OSError("down")

        saved_ai = userbot.ai
        userbot.ai = _Boom()
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        self.assertIn("скрыт", asyncio.run(userbot.sanitize_tool_output("secret", "m")))

    def test_refresh_models(self):
        async def _list():
            return SimpleNamespace(
                data=[SimpleNamespace(id="m1"), SimpleNamespace(id="m2")]
            )

        saved_ai = userbot.ai
        saved_models = list(userbot.MODELS)
        userbot.ai = SimpleNamespace(models=SimpleNamespace(list=_list))
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        self.addCleanup(setattr, userbot, "MODELS", saved_models)
        asyncio.run(userbot.refresh_models())
        self.assertEqual(userbot.MODELS, ["m1", "m2"])

    def test_verify_unrestricted_short_circuit(self):
        self.assertTrue(
            asyncio.run(
                userbot.verify_tool_call(
                    "run_shell", {"command": "rm -rf /"}, "m", True
                )
            )
        )

    def test_sanitize_reports_empty_response(self):
        saved_ai = userbot.ai
        userbot.ai = NonStreamAI(SimpleNamespace(choices=[]))
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        self.assertEqual(
            asyncio.run(userbot.sanitize_tool_output("secret", "m")),
            "[вывод скрыт: пустой ответ санитайзера]",
        )

    def test_sanitize_joins_list_content(self):
        saved_ai = userbot.ai
        userbot.ai = NonStreamAI(
            NonStreamResponse(NonStreamMessage(content=["AL", "LOW"]))
        )
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        self.assertEqual(
            asyncio.run(userbot.sanitize_tool_output("secret", "m")), "ALLOW"
        )

    def test_sanitize_hides_blank_content(self):
        saved_ai = userbot.ai
        userbot.ai = NonStreamAI(NonStreamResponse(NonStreamMessage(content="   ")))
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        self.assertEqual(
            asyncio.run(userbot.sanitize_tool_output("secret", "m")),
            "[вывод скрыт: пустой ответ санитайзера]",
        )

    def test_get_sender_returns_none_on_error(self):
        class _Boom:
            sender_id = 3

            async def get_sender(self):
                raise RPCError(request=None, message="down")

        self.assertIsNone(asyncio.run(userbot._get_sender(_Boom())))
        self.assertFalse(asyncio.run(userbot._sender_is_bot(_Boom())))

    def test_get_sender_label_without_names(self):
        class _Event:
            sender_id = 77

            def __init__(self, username):
                self._username = username

            async def get_sender(self):
                return SimpleNamespace(
                    first_name="", last_name="", username=self._username
                )

        self.assertEqual(asyncio.run(userbot.get_sender_label(_Event(None))), "77")
        self.assertEqual(asyncio.run(userbot.get_sender_label(_Event("only"))), "@only")

    def test_fetch_live_messages_reports_failure(self):
        class _Boom:
            async def get_messages(self, chat_id, limit=20):
                raise RPCError(request=None, message="down")

        self.assertEqual(asyncio.run(userbot.fetch_live_messages(1, 5, _Boom())), [])

    def test_fetch_live_messages_skips_empty_and_reverses(self):
        class _Client:
            async def get_messages(self, chat_id, limit=20):
                return [
                    SimpleNamespace(message="свежее", out=True),
                    SimpleNamespace(message="старое", out=False),
                    SimpleNamespace(message="   ", out=False),
                    SimpleNamespace(message=None, out=False),
                ]

        out = asyncio.run(userbot.fetch_live_messages(1, 5, _Client()))
        self.assertEqual(
            out,
            [
                {"role": "user", "content": "старое"},
                {"role": "assistant", "content": "свежее"},
            ],
        )

    def test_refresh_models_reports_api_error(self):
        async def _list():
            raise OSError("danyapi down")

        saved_ai = userbot.ai
        saved_models = userbot.MODELS
        userbot.ai = SimpleNamespace(models=SimpleNamespace(list=_list))
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        self.addCleanup(setattr, userbot, "MODELS", saved_models)
        with self.assertLogs("danybot", level="WARNING") as captured:
            asyncio.run(userbot.refresh_models())
        self.assertTrue(userbot.MODELS)
        self.assertIn("Не удалось загрузить модели", captured.output[0])

    def test_disconnect_quietly_closes_client(self):
        class _Client:
            def __init__(self):
                self.disconnected = 0

            async def disconnect(self):
                self.disconnected += 1
                return True

        class _Boom(_Client):
            async def disconnect(self):
                raise RuntimeError("down")

        saver = _FakeSaver()
        saved_saver = userbot.HISTORY_SAVER
        saved_client = userbot.client
        userbot.HISTORY_SAVER = saver
        self.addCleanup(setattr, userbot, "HISTORY_SAVER", saved_saver)
        self.addCleanup(setattr, userbot, "client", saved_client)
        client = _Client()
        userbot.client = client
        asyncio.run(userbot.disconnect_quietly())
        self.assertEqual(client.disconnected, 1)
        self.assertEqual(saver.flushed, 1)
        userbot.client = _Boom()
        asyncio.run(userbot.disconnect_quietly())
        userbot.client = None
        asyncio.run(userbot.disconnect_quietly())

    def test_close_ai_survives_client_error(self):
        class _AI:
            def __init__(self):
                self.closed = 0

            async def close(self):
                self.closed += 1
                return True

        class _Boom:
            async def close(self):
                raise RuntimeError("down")

        saved_ai = userbot.ai
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        fake = _AI()
        userbot.ai = fake
        asyncio.run(userbot.close_ai())
        self.assertEqual(fake.closed, 1)
        userbot.ai = _Boom()
        asyncio.run(userbot.close_ai())

    def test_proxy_candidates_report_failure(self):
        async def boom(*_args, **_kwargs):
            raise OSError("no network")

        with (
            mock.patch.object(proxies, "get_proxy_candidates", boom),
            self.assertLogs("danybot", level="WARNING") as captured,
        ):
            self.assertEqual(asyncio.run(userbot._proxy_candidates()), [])
        self.assertIn("Не удалось получить прокси", captured.output[0])

    def test_proxy_candidates_return_list(self):
        marker = [{"proxy_type": "socks5", "addr": "1.1.1.1", "port": 1080}]

        async def fake(*_args, **_kwargs):
            return list(marker)

        with mock.patch.object(proxies, "get_proxy_candidates", fake):
            self.assertEqual(asyncio.run(userbot._proxy_candidates()), marker)


class BotModuleTest(BotTestCase):
    def test_mention_helpers(self):
        saved = bot.bot_username
        bot.bot_username = "DanyBOTAPI_bot"
        self.addCleanup(setattr, bot, "bot_username", saved)
        self.assertTrue(bot._is_mentioned("hey @DanyBOTAPI_bot hi"))
        self.assertEqual(bot._strip_mention("hey @DanyBOTAPI_bot hi"), "hey  hi")

    def test_mention_none_without_username(self):
        saved = bot.bot_username
        bot.bot_username = ""
        self.addCleanup(setattr, bot, "bot_username", saved)
        self.assertFalse(bot._is_mentioned("@x"))

    def test_state_roundtrip(self):
        tmp = Path(tempfile.mkdtemp(prefix="danybot_botstate_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = bot.STATE_FILE
        bot.STATE_FILE = tmp / "state_bot.json"
        self.addCleanup(setattr, bot, "STATE_FILE", saved)
        saved_overrides = dict(bot.model_overrides)
        self.addCleanup(bot.model_overrides.clear)
        self.addCleanup(bot.model_overrides.update, saved_overrides)
        bot.model_overrides[1] = "m"
        bot.save_state()
        bot.model_overrides.clear()
        bot.load_state()
        self.assertEqual(bot.model_overrides.get(1), "m")

    def test_safe_reply_and_edit_text(self):
        event = _FakeReplyEvent()
        sent = asyncio.run(bot.safe_reply(event, "hi"))
        self.assertIsNotNone(sent)
        with mock.patch.object(bot, "get_bot_client", RichFakeClient):
            self.assertTrue(asyncio.run(bot.edit_text(1, 2, "t")))

    def test_disconnect_quietly_flushes_history(self):
        flushed = []

        class _Saver:
            def mark_dirty(self):
                pass

            async def flush(self):
                flushed.append(1)

        saved_saver = bot.HISTORY_SAVER
        saved_client = bot.bot_client
        bot.HISTORY_SAVER = _Saver()
        bot.bot_client = None
        try:
            asyncio.run(bot.disconnect_quietly())
        finally:
            bot.HISTORY_SAVER = saved_saver
            bot.bot_client = saved_client
        self.assertEqual(flushed, [1])


class _AnswerCallback:
    def __init__(self, data="settings:model", sender_id=6, message=None):
        self.data = data
        self.from_user = SimpleNamespace(id=sender_id)
        self.message = message
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return True


def _raise_later(exc):
    async def _coro():
        raise exc

    return _coro()


def _value_later(value):
    async def _coro():
        return value

    return _coro()


def _local_bot():
    return Bot(token="1234567890:" + "a" * 40)


def _aiogram_message(
    text="привет", chat_id=5, chat_type="private", user_id=6, reply=None
):
    return Message(
        message_id=11,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="A"),
        text=text,
        reply_to_message=reply,
    )


class BotAdapterTest(BotTestCase):
    def _client(self, **kwargs):
        inner = _FakeAiogramBot(**kwargs)
        return bot.BotClient(inner), inner

    def _patch(self, target, name, value) -> None:
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, name, result=None, error=None) -> list:
        calls = []

        async def _method(_self, *args, **kwargs):
            calls.append((args, kwargs))
            if error is not None:
                raise error
            return result

        self._patch(Message, name, _method)
        return calls

    def _record_answer(self, result=None, error=None) -> list:
        return self._record("answer", result=result, error=error)

    def _record_edit(self, result=None, error=None) -> list:
        return self._record("edit_text", result=result, error=error)

    def test_resolve_returns_bot_profile(self):
        client, _ = self._client()
        me = asyncio.run(client.resolve())
        self.assertEqual((me.id, me.username), (42, "danybot_bot"))

    def test_get_me_maps_profile_fields(self):
        client, _ = self._client()
        me = asyncio.run(client.get_me())
        self.assertEqual(me.id, 42)
        self.assertEqual(me.first_name, "Me")
        self.assertEqual(me.last_name, "M")
        self.assertIsNone(me.title)

    def test_get_entity_reports_chat_with_members(self):
        client, _ = self._client()
        entity = asyncio.run(client.get_entity(5150))
        self.assertEqual(entity.id, 5150)
        self.assertEqual(entity.title, "Chat T")
        self.assertEqual(entity.username, "uchat")
        self.assertEqual(entity.participants_count, 11)

    def test_get_entity_accepts_strings_and_handles(self):
        client, _ = self._client()
        self.assertEqual(asyncio.run(client.get_entity("5150")).id, 5150)
        self.assertEqual(asyncio.run(client.get_entity("danychat")).id, 7)

    def test_get_entity_survives_member_count_error(self):
        client, _ = self._client(count_error=TelegramBadRequest(_NO_METHOD, "no count"))
        entity = asyncio.run(client.get_entity(-1))
        self.assertEqual(entity.id, -1)
        self.assertIsNone(entity.participants_count)

    def test_get_entity_reports_api_error_as_rpc(self):
        client, _ = self._client(chat_error=TelegramBadRequest(_NO_METHOD, "no chat"))
        with self.assertRaises(RPCError):
            asyncio.run(client.get_entity(1))

    def test_edit_message_uses_bot_api(self):
        client, inner = self._client()
        asyncio.run(client.edit_message(7, 8, "текст"))
        self.assertEqual(inner.edits, [(7, 8, "текст")])

    def test_edit_message_error_becomes_rpc(self):
        class _Boom:
            async def edit_message_text(self, **_kwargs):
                raise TelegramBadRequest(_NO_METHOD, "message not found")

        with self.assertRaises(RPCError):
            asyncio.run(bot.BotClient(_Boom()).edit_message(1, 2, "t"))

    def test_set_commands_uses_menu(self):
        client, inner = self._client()
        asyncio.run(client.set_commands())
        self.assertEqual([item.command for item in inner.commands], menu_commands())

    def test_set_commands_survives_api_error(self):
        client, _ = self._client(
            commands_error=TelegramBadRequest(_NO_METHOD, "no commands")
        )
        self.assertIsNone(asyncio.run(client.set_commands()))

    def test_poll_keeps_session_and_signals_with_us(self):
        recorded = {}

        class _FakeDispatcher:
            async def start_polling(self, target, **kwargs):
                recorded["target"] = target
                recorded["kwargs"] = kwargs

        client, inner = self._client()
        saved = bot.build_dispatcher
        self.addCleanup(setattr, bot, "build_dispatcher", saved)
        bot.build_dispatcher = _FakeDispatcher
        asyncio.run(client.poll())
        self.assertIs(recorded["target"], inner)
        self.assertFalse(recorded["kwargs"]["handle_signals"])
        self.assertFalse(recorded["kwargs"]["close_bot_session"])

    def test_close_closes_session(self):
        client, inner = self._client()
        asyncio.run(client.close())
        self.assertEqual(inner.session.closed, 1)

    def test_typing_action_sends_chat_action(self):
        client, inner = self._client()

        async def scenario():
            async with client.action(7, "typing"):
                await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(scenario())
        self.assertEqual(inner.actions, [(7, "typing")])

    def test_typing_action_survives_outer_cancellation(self):
        client, inner = self._client()

        async def scenario():
            async def body():
                async with client.action(7, "typing"):
                    await asyncio.sleep(5)

            with self.assertRaises(asyncio.CancelledError):
                task = asyncio.create_task(body())
                await asyncio.sleep(0.01)
                task.cancel()
                await task

        asyncio.run(scenario())
        self.assertEqual(inner.actions, [(7, "typing")])

    def test_typing_action_stops_after_repeated_errors(self):
        attempts = []

        class _Boom:
            async def send_chat_action(self, chat_id, action):
                attempts.append((chat_id, action))
                raise TelegramBadRequest(_NO_METHOD, "chat not found")

        action = bot.TypingAction(_Boom(), 7, "typing", interval=0.01)

        async def scenario():
            async with action:
                await asyncio.sleep(0.05)

        asyncio.run(scenario())
        self.assertEqual(attempts, [(7, "typing")] * bot.TYPING_ATTEMPTS)

    def test_typing_action_survives_transient_error(self):
        attempts = []

        class _Flaky:
            async def send_chat_action(self, chat_id, action):
                attempts.append((chat_id, action))
                if len(attempts) <= 2:
                    raise TelegramNetworkError(_NO_METHOD, "reset")
                return True

        action = bot.TypingAction(_Flaky(), 7, "typing", interval=0.001)

        async def scenario():
            async with action:
                for _ in range(3000):
                    if len(attempts) >= bot.TYPING_ATTEMPTS + 3:
                        return
                    await asyncio.sleep(0.001)

        asyncio.run(scenario())
        self.assertGreaterEqual(len(attempts), bot.TYPING_ATTEMPTS + 3)
        self.assertTrue(all(item == (7, "typing") for item in attempts))

    def test_typing_action_without_enter_is_noop(self):
        action = bot.TypingAction(_FakeAiogramBot(), 7, "typing")
        asyncio.run(action.__aexit__())

    def test_retry_after_becomes_flood_wait(self):
        exc = TelegramRetryAfter(_NO_METHOD, "Too Many Requests", retry_after=7)

        async def scenario():
            with self.assertRaises(FloodWaitError) as ctx:
                await bot._tg_call(_raise_later(exc))
            return ctx.exception.seconds

        self.assertEqual(asyncio.run(scenario()), 7)

    def test_api_error_becomes_rpc_error(self):
        async def scenario():
            with self.assertRaises(RPCError):
                await bot._tg_call(
                    _raise_later(TelegramBadRequest(_NO_METHOD, "bad request"))
                )

        asyncio.run(scenario())

    def test_network_error_becomes_os_error(self):
        async def scenario():
            with self.assertRaises(OSError):
                await bot._tg_call(
                    _raise_later(TelegramNetworkError(_NO_METHOD, "down"))
                )

        asyncio.run(scenario())

    def test_tg_call_returns_result(self):
        self.assertEqual(asyncio.run(bot._tg_call(_value_later(5))), 5)

    def test_event_reads_chat_and_sender(self):
        event = bot.BotEvent(_aiogram_message())
        self.assertEqual(event.chat_id, 5)
        self.assertEqual(event.sender_id, 6)
        self.assertTrue(event.is_private)
        self.assertEqual(event.message.message, "привет")
        self.assertEqual(event.message.id, 11)
        self.assertFalse(event.message.out)
        self.assertFalse(event.message.is_reply)
        self.assertEqual(asyncio.run(event.get_sender()).id, 6)

    def test_event_in_group_is_not_private(self):
        event = bot.BotEvent(_aiogram_message(chat_id=-100, chat_type="supergroup"))
        self.assertFalse(event.is_private)
        self.assertEqual(event.chat_id, -100)

    def test_event_without_sender_fails_closed(self):
        message = _aiogram_message().model_copy(update={"from_user": None})
        self.assertEqual(bot.BotEvent(message).sender_id, 0)

    def test_event_exposes_replied_message(self):
        replied = _aiogram_message(text="ответ бота", user_id=77)
        event = bot.BotEvent(_aiogram_message(reply=replied))
        self.assertTrue(event.message.is_reply)
        wrapped = asyncio.run(event.message.get_reply_message())
        self.assertIsNotNone(wrapped)
        self.assertEqual(cast(Any, wrapped).message, "ответ бота")
        self.assertEqual(cast(Any, wrapped).sender_id, 77)

    def test_event_without_reply_returns_none(self):
        event = bot.BotEvent(_aiogram_message())
        self.assertIsNone(asyncio.run(event.message.get_reply_message()))

    def test_event_reply_wraps_sent_message(self):
        calls = self._record_answer(result=SimpleNamespace(message_id=99))
        sent = asyncio.run(
            bot.BotEvent(_aiogram_message()).reply("ответ", buttons="kb")
        )
        self.assertIsNotNone(sent)
        self.assertEqual(cast(Any, sent).id, 99)
        self.assertEqual(calls, [(("ответ",), {"reply_markup": "kb"})])

    def test_event_reply_error_becomes_rpc(self):
        self._record_answer(error=TelegramBadRequest(_NO_METHOD, "blocked"))
        with self.assertRaises(RPCError):
            asyncio.run(bot.BotEvent(_aiogram_message()).reply("x"))

    def test_callback_event_routes_answer_edit_reply(self):
        answers = self._record_answer(result=SimpleNamespace(message_id=99))
        edits = self._record_edit(result=SimpleNamespace(message_id=11))
        query = _AnswerCallback(message=_aiogram_message(text="меню"))
        event = bot.BotCallbackEvent(cast(Any, query))
        self.assertEqual(event.chat_id, 5)
        self.assertEqual(event.sender_id, 6)
        self.assertEqual(event.data, "settings:model")
        asyncio.run(event.answer("готово", alert=True))
        asyncio.run(event.edit("меню", buttons="kb"))
        sent = asyncio.run(event.reply("ответ"))
        self.assertEqual(query.answers, [("готово", True)])
        self.assertEqual(edits, [((), {"text": "меню", "reply_markup": "kb"})])
        self.assertEqual(answers, [(("ответ",), {"reply_markup": None})])
        self.assertIsNotNone(sent)
        self.assertEqual(cast(Any, sent).id, 99)

    def test_callback_event_edit_error_becomes_rpc(self):
        self._record_edit(error=TelegramBadRequest(_NO_METHOD, "not modified"))
        event = bot.BotCallbackEvent(
            cast(Any, _AnswerCallback(message=_aiogram_message()))
        )
        with self.assertRaises(RPCError):
            asyncio.run(event.edit("x"))

    def test_callback_event_without_message(self):
        event = bot.BotCallbackEvent(cast(Any, _AnswerCallback(message=None)))
        self.assertIsNone(event.chat_id)
        asyncio.run(event.answer("x"))
        asyncio.run(event.edit("y"))
        self.assertIsNone(asyncio.run(event.reply("z")))

    def test_callback_event_with_inaccessible_message(self):
        inaccessible = InaccessibleMessage(
            message_id=3, chat=Chat(id=5, type="private")
        )
        event = bot.BotCallbackEvent(cast(Any, _AnswerCallback(message=inaccessible)))
        asyncio.run(event.edit("y"))
        self.assertIsNone(asyncio.run(event.reply("z")))

    def test_dispatcher_feeds_message_to_handler(self):
        seen: list[Any] = []
        saved = bot.handler
        self.addCleanup(setattr, bot, "handler", saved)

        async def handler(event):
            seen.append(event)

        bot.handler = handler
        dispatcher = bot.build_dispatcher()
        asyncio.run(
            dispatcher.feed_update(
                _local_bot(), Update(update_id=1, message=_aiogram_message())
            )
        )
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], bot.BotEvent)
        self.assertEqual(seen[0].chat_id, 5)

    def test_dispatcher_feeds_callback_to_handler(self):
        seen: list[Any] = []
        saved = bot.callback_handler
        self.addCleanup(setattr, bot, "callback_handler", saved)

        async def callback_handler(event):
            seen.append(event)

        bot.callback_handler = callback_handler
        query = CallbackQuery(
            id="q1",
            chat_instance="ci",
            from_user=User(id=8, is_bot=False, first_name="A"),
            data="settings:model",
            message=_aiogram_message(text="меню", user_id=6),
        )
        dispatcher = bot.build_dispatcher()
        asyncio.run(
            dispatcher.feed_update(
                _local_bot(), Update(update_id=2, callback_query=query)
            )
        )
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], bot.BotCallbackEvent)
        self.assertEqual((seen[0].chat_id, seen[0].sender_id), (5, 8))

    def test_dispatcher_feeds_inline_query_to_handler(self):
        seen: list[Any] = []
        saved = bot.inline_handler
        self.addCleanup(setattr, bot, "inline_handler", saved)

        async def inline_handler(event):
            seen.append(event)

        bot.inline_handler = inline_handler
        query = InlineQuery(
            id="iq1",
            from_user=User(id=8, is_bot=False, first_name="A"),
            query="привет",
            offset="",
        )
        dispatcher = bot.build_dispatcher()
        asyncio.run(
            dispatcher.feed_update(
                _local_bot(), Update(update_id=3, inline_query=query)
            )
        )
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], bot.BotInlineEvent)
        self.assertEqual((seen[0].inline_query_id, seen[0].query), ("iq1", "привет"))

    def test_dispatcher_requests_inline_query_updates(self):
        dispatcher = bot.build_dispatcher()
        self.assertIn("inline_query", dispatcher.resolve_used_update_types())
        self.assertIn("message", dispatcher.resolve_used_update_types())
        self.assertIn("callback_query", dispatcher.resolve_used_update_types())

    def test_chat_ref_normalizes_input(self):
        self.assertEqual(bot._chat_ref(5), 5)
        self.assertEqual(bot._chat_ref(" -100123 "), -100123)
        self.assertEqual(bot._chat_ref("@chat"), "@chat")
        self.assertEqual(bot._chat_ref(None), "None")

    def test_proxy_url_converts_schemes(self):
        self.assertEqual(
            bot._proxy_url({"proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080}),
            "socks5://1.2.3.4:1080",
        )
        self.assertEqual(
            bot._proxy_url({"proxy_type": "http", "addr": "1.2.3.4", "port": 8080}),
            "http://1.2.3.4:8080",
        )
        self.assertEqual(
            bot._proxy_url({"proxy_type": "weird", "addr": "h", "port": 1}),
            "socks5://h:1",
        )
        self.assertIsNone(bot._proxy_url({}))
        self.assertIsNone(bot._proxy_url(None))

    def test_connect_builds_client_for_proxy(self):
        saved_token = userbot.BOT_TOKEN
        self.addCleanup(setattr, userbot, "BOT_TOKEN", saved_token)
        userbot.BOT_TOKEN = "1234567890:" + "b" * 40
        built = []
        saved = bot.Bot
        self.addCleanup(setattr, bot, "Bot", saved)

        def _bot(token, session=None):
            built.append((token, getattr(session, "_proxy", None)))
            return _FakeAiogramBot()

        bot.Bot = _bot
        client = bot._connect({"proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080})
        self.assertIsInstance(client, bot.BotClient)
        self.assertEqual(built, [(userbot.BOT_TOKEN, "socks5://1.2.3.4:1080")])

    def test_connect_without_proxy_has_no_proxy_url(self):
        saved_token = userbot.BOT_TOKEN
        self.addCleanup(setattr, userbot, "BOT_TOKEN", saved_token)
        userbot.BOT_TOKEN = "1234567890:" + "b" * 40
        built = []
        saved = bot.Bot
        self.addCleanup(setattr, bot, "Bot", saved)

        def _bot(token, session=None):
            built.append((token, getattr(session, "_proxy", None)))
            return _FakeAiogramBot()

        bot.Bot = _bot
        bot._connect(None)
        self.assertEqual(built, [(userbot.BOT_TOKEN, None)])

    def test_get_bot_client_is_cached(self):
        saved_token = userbot.BOT_TOKEN
        self.addCleanup(setattr, userbot, "BOT_TOKEN", saved_token)
        userbot.BOT_TOKEN = "1234567890:" + "c" * 40
        saved_client = bot.bot_client
        self.addCleanup(setattr, bot, "bot_client", saved_client)
        bot.bot_client = None
        first = bot.get_bot_client()
        self.assertIs(bot.get_bot_client(), first)
        asyncio.run(first.close())

    def test_disconnect_closes_active_client(self):
        client, inner = self._client()
        saved_client = bot.bot_client
        self.addCleanup(setattr, bot, "bot_client", saved_client)
        bot.bot_client = client
        asyncio.run(bot.disconnect_quietly())
        self.assertEqual(inner.session.closed, 1)


class _BoomTools:
    def __init__(self, handler):
        self._handler = handler

    def __getattr__(self, name):
        return self._handler


class SubagentsTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self._orig = dict(subagents._RUNTIME)

        def restore():
            subagents._RUNTIME.update(self._orig)

        self.addCleanup(restore)

    class _AI:
        def __init__(self, content="done", tool_calls=None):
            self._content = content
            self._tool_calls = tool_calls

            class _Completions:
                def __init__(self, outer):
                    self._outer = outer

                async def create(self, **kwargs):
                    message = SimpleNamespace(
                        content=self._outer._content,
                        tool_calls=self._outer._tool_calls,
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=message)])

            self.chat = SimpleNamespace(completions=_Completions(self))

    class _ToolAI:
        def __init__(self, rounds):
            self._rounds = [list(r) for r in rounds]
            self.seen = []
            outer = self

            class _Completions:
                async def create(self, **kwargs):
                    outer.seen.append(kwargs["messages"])
                    content, tool_calls = outer._rounds.pop(0)
                    message = SimpleNamespace(content=content, tool_calls=tool_calls)
                    return SimpleNamespace(choices=[SimpleNamespace(message=message)])

            self.chat = SimpleNamespace(completions=_Completions())

    @staticmethod
    def _tool_call(call_id, name, arguments):
        return SimpleNamespace(
            id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
        )

    @staticmethod
    def _tool_messages(ai):
        return [m for m in ai.seen[-1] if m.get("role") == "tool"]

    @staticmethod
    async def _allow(name, arguments, model):
        return True

    @staticmethod
    async def _deny(name, arguments, model):
        return False

    def test_configure_and_is_configured(self):
        subagents.configure(ai=object(), model="m", enabled=True)
        self.assertTrue(subagents.is_configured())
        subagents.configure(enabled=False)
        self.assertFalse(subagents.is_configured())

    def test_select_tools(self):
        all_tools = subagents._select_tools(None)
        self.assertTrue(all_tools)
        picked = subagents._select_tools(["evaluate"])
        self.assertTrue(any(t["function"]["name"] == "evaluate" for t in picked))

    def test_select_tools_follows_schema_changes(self):
        extra = {
            "type": "function",
            "function": {"name": "probe_time", "parameters": {"type": "object"}},
        }
        with mock.patch.object(tools_module, "TOOLS", [*tools_module.TOOLS, extra]):
            names = [t["function"]["name"] for t in subagents._select_tools(None)]
        self.assertIn("probe_time", names)
        names_after = [t["function"]["name"] for t in subagents._select_tools(None)]
        self.assertNotIn("probe_time", names_after)

    def test_select_tools_unrestricted_inherits_session_tools(self):
        names = {t["function"]["name"] for t in subagents._select_tools(None, True)}
        self.assertTrue(names & set(tools_module.FILE_TOOL_NAMES))
        self.assertIn("run_shell", names)
        self.assertIn("save_skill", names)
        self.assertNotIn("run_subagent", names)
        picked = {
            t["function"]["name"] for t in subagents._select_tools(["read_file"], True)
        }
        self.assertEqual(picked, {"read_file"})

    def test_call_tool_unrestricted_bypasses_verifier_and_owner_gate(self):
        seen = {}

        async def fake_execute(
            name, args, chat_id, client, stats, unrestricted, allowed
        ):
            seen["name"] = name
            seen["unrestricted"] = unrestricted
            return "готово"

        async def boom(*_args, **_kwargs):
            raise AssertionError("верификатор не должен вызываться")

        saved = tools_module.execute_tool
        tools_module.execute_tool = fake_execute
        self.addCleanup(setattr, tools_module, "execute_tool", saved)
        result = asyncio.run(
            subagents._call_tool(
                tools_module,
                boom,
                "run_shell",
                {"command": "echo hi"},
                "m",
                1,
                None,
                None,
                {"run_shell"},
                True,
                True,
            )
        )
        self.assertEqual(result[1], "готово")
        self.assertTrue(seen["unrestricted"])
        self.assertEqual(seen["name"], "run_shell")

    def test_unrestricted_subagent_runs_owner_tool(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "get_time", "{}")]),
                ("время получено", None),
            ]
        )
        subagents.configure(ai=ai, model="m", verifier=self._deny, enabled=True)
        result = asyncio.run(
            subagents.run_subagent("скажи время", verify=True, unrestricted=True)
        )
        self.assertEqual(result["result"], "время получено")
        self.assertEqual(result["tools_used"], ["get_time"])

    def test_loads_and_assistant_message(self):
        self.assertEqual(subagents._loads(""), {})
        self.assertEqual(subagents._loads("nope"), {})
        self.assertEqual(subagents._loads('{"a": 1}'), {"a": 1})
        tc = SimpleNamespace(
            id="c1", function=SimpleNamespace(name="f", arguments="{}")
        )
        msg = subagents._assistant_message("c", [tc])
        self.assertEqual(msg["tool_calls"][0]["id"], "c1")

    def test_run_subagent_ok(self):
        subagents.configure(
            ai=self._AI("answer"), model="m", verifier=None, enabled=True
        )
        result = asyncio.run(subagents.run_subagent("do it"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], "answer")

    def test_run_subagent_empty_task(self):
        subagents.configure(ai=self._AI(), model="m", enabled=True)
        result = asyncio.run(subagents.run_subagent(""))
        self.assertFalse(result["ok"])

    def test_run_subagent_not_configured(self):
        subagents.configure(enabled=False)
        result = asyncio.run(subagents.run_subagent("x"))
        self.assertFalse(result["ok"])

    def test_run_subagents_parallel(self):
        subagents.configure(ai=self._AI("r"), model="m", verifier=None, enabled=True)
        results = asyncio.run(subagents.run_subagents(["a", "b"], concurrency=2))
        self.assertEqual(len(results), 2)

    def test_tool_runs_when_verifier_allows(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "evaluate", '{"expression":"2+2"}')]),
                ("итог 4", None),
            ]
        )
        subagents.configure(ai=ai, model="m", verifier=self._allow, enabled=True)
        result = asyncio.run(subagents.run_subagent("посчитай 2+2", verify=True))
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], "итог 4")
        self.assertEqual(result["tools_used"], ["evaluate"])
        self.assertEqual(self._tool_messages(ai)[0]["content"], "4")

    def test_tool_blocked_when_verifier_denies(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "evaluate", '{"expression":"2+2"}')]),
                ("не буду", None),
            ]
        )
        subagents.configure(ai=ai, model="m", verifier=self._deny, enabled=True)
        result = asyncio.run(subagents.run_subagent("посчитай 2+2", verify=True))
        self.assertEqual(result["tools_used"], [])
        self.assertEqual(
            self._tool_messages(ai)[0]["content"],
            "Вызов отклонён проверкой безопасности.",
        )

    def test_tool_runs_without_verifier(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "evaluate", '{"expression":"3*3"}')]),
                ("девять", None),
            ]
        )
        subagents.configure(ai=ai, model="m", verifier=None, enabled=True)
        result = asyncio.run(subagents.run_subagent("посчитай 3*3"))
        self.assertEqual(self._tool_messages(ai)[0]["content"], "9")
        self.assertEqual(result["tools_used"], ["evaluate"])

    def test_several_tools_in_one_round_keep_order(self):
        ai = self._ToolAI(
            [
                (
                    "",
                    [
                        self._tool_call("c1", "evaluate", '{"expression":"1+1"}'),
                        self._tool_call("c2", "get_time", "{}"),
                    ],
                ),
                ("готово", None),
            ]
        )
        subagents.configure(ai=ai, model="m", verifier=self._allow, enabled=True)
        result = asyncio.run(subagents.run_subagent("посчитай и дай время"))
        messages = self._tool_messages(ai)
        self.assertEqual([m["tool_call_id"] for m in messages], ["c1", "c2"])
        self.assertEqual(messages[0]["content"], "2")
        self.assertIn("utc", messages[1]["content"])
        self.assertEqual(result["tools_used"], ["evaluate", "get_time"])

    def test_broken_tool_arguments_do_not_stop_round(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "evaluate", "не json")]),
                ("готово", None),
            ]
        )
        subagents.configure(ai=ai, model="m", verifier=self._allow, enabled=True)
        result = asyncio.run(subagents.run_subagent("посчитай"))
        self.assertTrue(result["ok"])
        self.assertIn("Ошибка вычисления", self._tool_messages(ai)[0]["content"])

    def test_tool_outside_allowed_set_is_refused(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "read_file", '{"path":"x"}')]),
                ("готово", None),
            ]
        )
        subagents.configure(ai=ai, model="m", verifier=self._allow, enabled=True)
        result = asyncio.run(subagents.run_subagent("прочитай файл"))
        self.assertEqual(result["tools_used"], ["read_file"])
        self.assertIn("недоступен в этой сессии", self._tool_messages(ai)[0]["content"])

    def test_verifier_crash_is_treated_as_denial(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "evaluate", '{"expression":"2+2"}')]),
                ("готово", None),
            ]
        )

        async def crash(name, arguments, model):
            raise ValueError("verifier down")

        subagents.configure(ai=ai, model="m", verifier=crash, enabled=True)
        result = asyncio.run(subagents.run_subagent("посчитай 2+2"))
        self.assertEqual(result["tools_used"], [])
        self.assertIn("отклонён", self._tool_messages(ai)[0]["content"])

    def test_long_loop_keeps_context_bounded(self):
        rounds = 30
        ai = self._ToolAI(
            [
                ("", [self._tool_call(f"c{index}", "get_time", "{}")])
                for index in range(rounds)
            ]
            + [("готово", None)]
        )
        subagents.configure(
            ai=ai,
            model="m",
            verifier=self._allow,
            enabled=True,
            max_rounds=rounds + 2,
        )
        result = asyncio.run(subagents.run_subagent("долгая задача"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["rounds"], rounds + 1)
        final = ai.seen[-1]
        self.assertLessEqual(len(final), subagents.MAX_CONTEXT_MESSAGES)
        self.assertGreater(len(final), subagents.MAX_CONTEXT_MESSAGES // 2)
        self.assertLess(len([m for m in final if m.get("role") == "tool"]), rounds)
        self.assertEqual(final[0]["role"], "system")
        self.assertEqual(final[1]["content"], "долгая задача")

    def test_tool_output_truncation_is_marked(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "evaluate", '{"expression":"1+1"}')]),
                ("готово", None),
            ]
        )

        async def fake_execute(
            name, arguments, chat_id, client, stats, unrestricted, a
        ):
            return "z" * (subagents.MAX_TOOL_RESULT + 500)

        saved = tools_module.execute_tool
        self.addCleanup(setattr, tools_module, "execute_tool", saved)
        tools_module.execute_tool = fake_execute
        subagents.configure(ai=ai, model="m", verifier=self._allow, enabled=True)
        asyncio.run(subagents.run_subagent("посчитай"))
        content = self._tool_messages(ai)[0]["content"]
        self.assertTrue(content.endswith(subagents.TRUNCATED_MARK))
        self.assertEqual(
            len(content), subagents.MAX_TOOL_RESULT + len(subagents.TRUNCATED_MARK)
        )

    def test_failing_worker_does_not_kill_others(self):
        async def explode(spec):
            raise RuntimeError("subagent down")

        async def fine(spec):
            return {"name": "fine", "task": spec["task"], "ok": True}

        async def run():
            mixed = await subagents._gather_workers(
                explode, [{"name": "bad", "task": "a"}, {"name": "bad", "task": "b"}]
            )
            healthy = await subagents._gather_workers(fine, [{"task": "c"}])
            return mixed, healthy

        mixed, healthy = asyncio.run(run())
        self.assertEqual(len(mixed), 2)
        self.assertTrue(all(not item["ok"] for item in mixed))
        self.assertIn("Ошибка субагента", mixed[0]["result"])
        self.assertTrue(healthy[0]["ok"])
        self.assertEqual(healthy[0]["task"], "c")

    def test_cancelled_tool_call_inside_subagent_is_reraised(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "evaluate", '{"expression":"2+2"}')]),
                ("готово", None),
            ]
        )

        async def cancel(*_args, **_kwargs):
            raise asyncio.CancelledError

        saved = tools_module.execute_tool
        tools_module.execute_tool = cancel
        self.addCleanup(setattr, tools_module, "execute_tool", saved)
        subagents.configure(ai=ai, model="m", verifier=self._allow, enabled=True)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(subagents.run_subagent("посчитай"))

    def test_cancelled_worker_is_not_swallowed(self):
        async def cancel(spec):
            raise asyncio.CancelledError

        async def run():
            await subagents._gather_workers(cancel, [{"name": "a", "task": "t"}])

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run())

    def test_task_spec_cannot_escalate_privileges(self):
        seen = {}

        async def fake_run_subagent(task, **kwargs):
            seen["task"] = task
            seen.update(kwargs)
            return {
                "name": kwargs.get("subagent_name", ""),
                "task": task,
                "ok": True,
                "rounds": 1,
                "tools_used": [],
                "result": "x",
            }

        spec = {
            "task": "что-то",
            "unrestricted": True,
            "chat_id": -999,
            "model": "чужая-модель",
            "max_rounds": 10**6,
            "name": "N" * 500,
            "tools": "read_file",
        }
        saved = subagents.run_subagent
        subagents.run_subagent = fake_run_subagent
        self.addCleanup(setattr, subagents, "run_subagent", saved)
        asyncio.run(
            subagents.run_subagents(
                [spec],
                chat_id=42,
                client="real-client",
                model="real-model",
                verify=True,
                unrestricted=False,
            )
        )
        self.assertEqual(seen["task"], "что-то")
        self.assertEqual(seen["chat_id"], 42)
        self.assertEqual(seen["client"], "real-client")
        self.assertEqual(seen["model"], "real-model")
        self.assertFalse(seen["unrestricted"])
        self.assertTrue(seen["verify"])
        self.assertEqual(seen["max_rounds"], subagents.MAX_ROUNDS)
        self.assertEqual(seen["tool_names"], ["read_file"])
        self.assertEqual(len(seen["subagent_name"]), subagents.MAX_NAME_CHARS)

    def test_no_round_limit_by_default(self):
        rounds = 12
        ai = self._ToolAI(
            [
                ("", [self._tool_call(f"c{index}", "get_time", "{}")])
                for index in range(rounds)
            ]
            + [("готово", None)]
        )
        subagents.configure(
            ai=ai, model="m", verifier=self._allow, enabled=True, max_rounds=None
        )
        subagents._RUNTIME["max_rounds"] = None
        result = asyncio.run(subagents.run_subagent("долгая задача"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["rounds"], rounds + 1)

    def test_model_rounds_clamps_to_positive_int(self):
        self.assertEqual(subagents._model_rounds(None, 7), 7)
        self.assertEqual(subagents._model_rounds("12", 7), 12)
        self.assertEqual(subagents._model_rounds("nope", 7), 7)
        self.assertEqual(subagents._model_rounds(0, 7), 1)
        self.assertEqual(subagents._model_rounds(-5, 7), 1)

    def test_tool_names_normalises_input(self):
        self.assertIsNone(subagents._tool_names(None))
        self.assertIsNone(subagents._tool_names(5))
        self.assertIsNone(subagents._tool_names([]))
        self.assertEqual(subagents._tool_names(" read_file "), ["read_file"])
        self.assertEqual(subagents._tool_names(["a", " b ", 5]), ["a", "b", "5"])

    def test_configure_clamps_bounds(self):
        subagents.configure(concurrency=0, max_tokens=1, timeout=0, max_rounds=0)
        self.assertEqual(subagents._RUNTIME["concurrency"], 1)
        self.assertEqual(subagents._RUNTIME["max_tokens"], 64)
        self.assertEqual(subagents._RUNTIME["timeout"], 1.0)
        self.assertEqual(subagents._RUNTIME["max_rounds"], 1)

    def test_call_tool_wraps_tool_crash(self):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("tool down")

        name, text, ran = asyncio.run(
            subagents._call_tool(
                _BoomTools(boom), None, "evaluate", {}, "m", 1, None, None, None, False
            )
        )
        self.assertEqual(name, "evaluate")
        self.assertIn("tool down", text)
        self.assertTrue(ran)

    def test_run_subagent_reports_api_error(self):
        class Boom:
            def __getattr__(self, _name):
                raise OSError("api down")

        subagents.configure(ai=Boom(), model="m", verifier=None, enabled=True)
        result = asyncio.run(subagents.run_subagent("задача"))
        self.assertFalse(result["ok"])
        self.assertIn("Ошибка субагента", result["result"])

    def test_run_subagent_reports_tool_crash(self):
        ai = self._ToolAI(
            [
                ("", [self._tool_call("c1", "evaluate", "{}")]),
                ("готово", None),
            ]
        )

        async def boom(*_args, **_kwargs):
            raise ZeroDivisionError("div")

        saved = tools_module.execute_tool
        tools_module.execute_tool = boom
        self.addCleanup(setattr, tools_module, "execute_tool", saved)
        subagents.configure(ai=ai, model="m", verifier=self._allow, enabled=True)
        result = asyncio.run(subagents.run_subagent("считай"))
        self.assertEqual(result["tools_used"], [])
        self.assertIn(
            "Ошибка инструмента evaluate: div", self._tool_messages(ai)[0]["content"]
        )

    def test_run_subagent_reports_round_limit_without_answer(self):
        rounds = 4
        ai = self._ToolAI(
            [("", [self._tool_call(f"c{i}", "get_time", "{}")]) for i in range(rounds)]
        )
        subagents.configure(
            ai=ai, model="m", verifier=self._allow, enabled=True, max_rounds=rounds
        )
        result = asyncio.run(subagents.run_subagent("долгая"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["rounds"], rounds)
        self.assertEqual(result["result"], "Достигнут лимит шагов субагента.")

    def test_run_subagents_rejects_bad_input(self):
        self.assertEqual(asyncio.run(subagents.run_subagents(5)), [])
        self.assertEqual(asyncio.run(subagents.run_subagents([])), [])
        self.assertEqual(asyncio.run(subagents.run_subagents([None, 0, ""])), [])
        results = asyncio.run(subagents.run_subagents("одна задача"))
        self.assertEqual(len(results), 1)


class _FakeTelegramClient:
    def __init__(
        self,
        fail_times=0,
        error=None,
        username="danybot",
        uid=42,
        command_error=None,
    ):
        self.events = []
        self.proxies = []
        self.starts = 0
        self.commands = []
        self.disconnects = 0
        self.running = False
        self._fail = fail_times
        self._error = error
        self._username = username
        self._uid = uid
        self._command_error = command_error

    def add_event_handler(self, handler, event):
        self.events.append(event)

    def set_proxy(self, proxy):
        if self.running:
            raise RuntimeError("client is connected")
        self.proxies.append(proxy)

    async def start(self, *args, **kwargs):
        self.starts += 1
        if self._error is not None:
            raise self._error
        if self._fail > 0:
            self._fail -= 1
            raise OSError("connect failed")
        self.running = True
        return SimpleNamespace()

    async def get_me(self):
        return SimpleNamespace(
            first_name="Me", last_name="M", username=self._username, id=self._uid
        )

    async def run_until_disconnected(self):
        self.running = False

    async def disconnect(self):
        self.running = False
        self.disconnects += 1
        return True

    async def __call__(self, request):
        if self._command_error is not None:
            raise self._command_error
        self.commands.append(request)
        return SimpleNamespace()


class _NoMethod:
    def __repr__(self):
        return "no-method"


_NO_METHOD: Any = _NoMethod()


class _FakeAiogramClient:
    def __init__(self, error=None, username="danybot_bot", uid=77):
        self.error = error
        self.username = username
        self.uid = uid
        self.commands = 0
        self.polled = 0
        self.closed = 0

    async def resolve(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(username=self.username, id=self.uid)

    async def set_commands(self):
        self.commands += 1

    async def poll(self):
        self.polled += 1

    async def close(self):
        self.closed += 1


class _FakeBotSession:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


class _FakeAiogramBot:
    def __init__(
        self,
        commands_error=None,
        chat_error=None,
        count_error=None,
        inline_error=None,
    ):
        self.commands_error = commands_error
        self.chat_error = chat_error
        self.count_error = count_error
        self.inline_error = inline_error
        self.commands = []
        self.actions = []
        self.edits = []
        self.inline_calls = []
        self.session = _FakeBotSession()

    async def get_me(self):
        return SimpleNamespace(
            id=42,
            username="danybot_bot",
            first_name="Me",
            last_name="M",
            is_bot=True,
        )

    async def set_my_commands(self, commands):
        if self.commands_error is not None:
            raise self.commands_error
        self.commands = list(commands)
        return True

    async def answer_inline_query(
        self, inline_query_id=None, results=None, cache_time=None, is_personal=None
    ):
        if self.inline_error is not None:
            raise self.inline_error
        self.inline_calls.append(
            {
                "id": inline_query_id,
                "results": list(results or ()),
                "cache_time": cache_time,
                "is_personal": is_personal,
            }
        )
        return True

    async def get_chat(self, key):
        if self.chat_error is not None:
            raise self.chat_error
        return SimpleNamespace(
            id=int(key) if str(key).lstrip("-").isdigit() else 7,
            type="supergroup",
            title="Chat T",
            username="uchat",
        )

    async def get_chat_member_count(self, chat_id):
        if self.count_error is not None:
            raise self.count_error
        return 11

    async def edit_message_text(self, text=None, chat_id=None, message_id=None):
        self.edits.append((chat_id, message_id, text))
        return True

    async def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))
        return True


class UserbotHandlerTest(BotTestCase):
    DM = 777
    GROUP = -1002
    OWNER = 5

    def setUp(self):
        super().setUp()
        for attr, value in (
            ("model_overrides", {}),
            ("coder_chats", set()),
            ("reasoning_hidden", set()),
            ("tools_hidden", set()),
            ("recent_reply_ids", set()),
            ("seen_msg_keys", set()),
            ("last_chat_activity", {}),
            ("chat_history", {}),
        ):
            saved = getattr(userbot, attr)
            self.addCleanup(setattr, userbot, attr, saved)
            setattr(userbot, attr, value)
        self.saver = _FakeSaver()
        for attr, value in (
            ("OWNER_IDS", {self.OWNER}),
            ("COOLDOWN", 0),
            ("HISTORY_SAVER", self.saver),
        ):
            saved = getattr(userbot, attr)
            self.addCleanup(setattr, userbot, attr, saved)
            setattr(userbot, attr, value)
        self.client = FakeClient()
        self.stream_calls = []

        async def fake_stream(*_args, **kwargs):
            self.stream_calls.append(kwargs)
            return "ответ"

        for target, attr, value in (
            (core, "stream_answer", fake_stream),
            (userbot, "get_client", lambda: self.client),
        ):
            saved = getattr(target, attr)
            self.addCleanup(setattr, target, attr, saved)
            setattr(target, attr, value)

    def _run(self, event):
        asyncio.run(userbot.handler(event))
        return event

    def _event(self, text, **kwargs):
        chat_id = kwargs.pop("chat_id", self.DM)
        sender_id = kwargs.pop("sender_id", 9)
        is_private = kwargs.pop("is_private", True)
        return _FakeHandlerEvent(
            _FakeMessage(text, **kwargs),
            chat_id=chat_id,
            sender_id=sender_id,
            is_private=is_private,
        )

    def _group_prompt(self, chat_id=None):
        entries = [
            item["content"]
            for item in userbot.chat_history[chat_id or self.GROUP]
            if item["role"] == "user"
        ]
        return entries[-1] if entries else ""

    def test_task_journal_records_finished_run(self):
        self._run(self._event(".db привет", sender_id=self.OWNER))
        record = userbot.TASKS.get(self.DM) or {}
        self.assertEqual(record.get("status"), core.TASK_DONE)
        self.assertIn("привет", record.get("prompt", ""))
        self.assertTrue(record.get("owner"))
        self.assertFalse(record.get("coder"))
        self.assertEqual(record.get("rounds"), 0)

    def test_task_journal_records_failure(self):
        async def failing(*_args, **_kwargs):
            raise userbot.OpenAIError("boom")

        core.stream_answer = failing
        self._run(self._event(".db привет"))
        record = userbot.TASKS.get(self.DM) or {}
        self.assertEqual(record.get("status"), core.TASK_FAILED)
        self.assertEqual(record.get("reason"), "OpenAIError")

    def test_task_journal_records_interruption(self):
        started = asyncio.Event()

        async def slow(*_args, **_kwargs):
            started.set()
            await asyncio.sleep(5)
            return "долгий ответ"

        core.stream_answer = slow
        self.addCleanup(userbot.SESSIONS.cancel_all)

        async def scenario():
            task = asyncio.ensure_future(
                userbot.handler(self._event(".db долгий", chat_id=888))
            )
            await started.wait()
            userbot.SESSIONS.cancel(888, reason="тест")
            await _wait_done(task)
            self.assertEqual(_task_outcome(task), "None")

        asyncio.run(scenario())
        record = userbot.TASKS.get(888) or {}
        self.assertEqual(record.get("status"), core.TASK_INTERRUPTED)
        self.assertIn("прерван", record.get("reason", ""))

    def test_task_journal_counts_rounds_from_progress(self):
        seen = {}

        async def with_progress(*_args, **kwargs):
            progress = kwargs.get("progress_fn")
            seen["has"] = progress is not None
            if progress is not None:
                progress(5, 9, "")
            return "ответ"

        core.stream_answer = with_progress
        self._run(self._event(".db привет", chat_id=999))
        record = userbot.TASKS.get(999) or {}
        self.assertTrue(seen["has"])
        self.assertEqual(record.get("rounds"), 5)
        self.assertEqual(record.get("tools"), 9)

    def test_task_journal_marks_stopped_run(self):
        async def with_progress(*_args, **kwargs):
            progress = kwargs.get("progress_fn")
            cast(Any, progress)(4, 4, "Достигнут лимит циклов инструментов: 4.")
            return "Достигнут лимит циклов инструментов: 4."

        core.stream_answer = with_progress
        self._run(self._event(".db привет", chat_id=1000))
        record = userbot.TASKS.get(1000) or {}
        self.assertEqual(record.get("status"), core.TASK_STOPPED)
        self.assertIn("лимит циклов", record.get("reason", ""))

    def test_task_command_in_userbot(self):
        userbot.TASKS.begin(self.DM, "старая задача", model="m")
        userbot.TASKS.finish(self.DM, core.TASK_INTERRUPTED, reason="прервана")
        event = self._run(self._event(".db task", sender_id=self.OWNER))
        self.assertIn("Прервана", event.sent[0])
        self.assertIn("старая задача", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_falls_back_to_prompt_when_live_history_empty(self):
        saved = userbot.fetch_live_messages
        self.addCleanup(setattr, userbot, "fetch_live_messages", saved)

        async def empty_live(*_args, **_kwargs):
            return []

        userbot.fetch_live_messages = empty_live
        self._run(self._event(".db вопрос без живой истории"))
        messages = self.stream_calls[-1]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn("вопрос без живой истории", messages[1]["content"])

    def test_live_history_is_used_when_available(self):
        self._run(self._event(".db обычный вопрос"))
        messages = self.stream_calls[-1]["messages"]
        self.assertGreater(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")

    def test_owner_gets_full_tool_menu(self):
        self._run(self._event(".db привет", sender_id=self.OWNER))
        self.assertEqual(self.stream_calls[-1]["tools"], userbot.TOOLS)

    def test_stranger_gets_public_tool_menu(self):
        self._run(self._event(".db привет", sender_id=9))
        self.assertEqual(self.stream_calls[-1]["tools"], tools_module.PUBLIC_TOOLS)

    def test_empty_answer_is_not_stored_in_history(self):
        async def blank(*_args, **_kwargs):
            return ""

        core.stream_answer = blank
        self._run(self._event(".db привет"))
        history = [
            m for m in userbot.chat_history.get(self.DM, []) if m["role"] == "assistant"
        ]
        self.assertEqual(history, [])

    def test_empty_message_ignored(self):
        self._run(self._event(""))
        self.assertEqual(self.stream_calls, [])

    def test_message_without_object_ignored(self):
        event = self._event(".db привет")
        event.message = None
        self._run(event)
        self.assertEqual(self.stream_calls, [])

    def test_missing_chat_id_ignored(self):
        event = self._event(".db привет")
        event.chat_id = None
        self._run(event)
        self.assertEqual(self.stream_calls, [])

    def test_own_reply_id_is_skipped(self):
        userbot.recent_reply_ids.add((self.DM, 3))
        self._run(self._event(".db привет", msg_id=3))
        self.assertEqual(self.stream_calls, [])

    def test_same_id_in_another_chat_is_processed(self):
        userbot.recent_reply_ids.add((self.DM, 3))
        self._run(
            self._event(".db привет", msg_id=3, chat_id=self.GROUP, is_private=False)
        )
        self.assertEqual(len(self.stream_calls), 1)

    def test_message_from_bot_is_ignored(self):
        event = self._event(".db привет")

        async def bot_sender():
            return SimpleNamespace(
                first_name="Bot", last_name="", username="b", bot=True
            )

        event.get_sender = bot_sender
        self._run(event)
        self.assertEqual(self.stream_calls, [])

    def test_group_message_needs_trigger(self):
        self._run(self._event("просто текст", chat_id=self.GROUP, is_private=False))
        self.assertEqual(self.stream_calls, [])

    def test_both_aliases_trigger(self):
        for text, msg_id in ((".db привет", 1), (".ai привет", 2)):
            self._run(self._event(text, msg_id=msg_id))
        self.assertEqual(len(self.stream_calls), 2)

    def test_messages_use_live_context(self):
        self._run(self._event(".db привет", msg_id=4))
        messages = self.stream_calls[0]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["content"], "t1")
        self.assertEqual(self.client.history_limits, [userbot.LIVE_HISTORY_LIMIT])

    def test_trigger_is_stripped_from_prompt(self):
        self._run(
            self._event(".db  привет", msg_id=40, chat_id=self.GROUP, is_private=False)
        )
        self.assertTrue(self._group_prompt().endswith(": привет"))

    def test_replied_text_is_added_to_prompt(self):
        replied = _FakeMessage("старый вопрос", msg_id=6, out=True)
        self._run(
            self._event(
                ".db ответ",
                msg_id=5,
                is_reply=True,
                reply_msg=replied,
                chat_id=self.GROUP,
                is_private=False,
            )
        )
        prompt = self._group_prompt()
        self.assertIn("старый вопрос", prompt)
        self.assertIn("ответ", prompt)

    def test_prompt_is_truncated(self):
        saved = userbot.MAX_REQUEST_LEN
        self.addCleanup(setattr, userbot, "MAX_REQUEST_LEN", saved)
        userbot.MAX_REQUEST_LEN = 20
        self._run(
            self._event(
                ".db " + "я" * 200 + "ХВОСТ",
                msg_id=8,
                chat_id=self.GROUP,
                is_private=False,
            )
        )
        self.assertNotIn("ХВОСТ", self._group_prompt())

    def test_long_replied_text_does_not_eat_the_request(self):
        saved = userbot.MAX_REQUEST_LEN
        self.addCleanup(setattr, userbot, "MAX_REQUEST_LEN", saved)
        userbot.MAX_REQUEST_LEN = 200
        replied = _FakeMessage("Ц" * 5000, msg_id=11, out=True)
        self._run(
            self._event(
                ".db иди дальше",
                msg_id=12,
                is_reply=True,
                reply_msg=replied,
                chat_id=self.GROUP,
                is_private=False,
            )
        )
        prompt = self._group_prompt()
        self.assertIn("иди дальше", prompt)
        self.assertLessEqual(len(prompt), 260)

    def test_trigger_only_message_replies_with_replied_text(self):
        replied = _FakeMessage("только реплай", msg_id=9, out=True)
        self._run(
            self._event(
                ".db",
                msg_id=10,
                is_reply=True,
                reply_msg=replied,
                chat_id=self.GROUP,
                is_private=False,
            )
        )
        self.assertTrue(self._group_prompt().endswith(": только реплай"))

    def test_empty_prompt_without_reply_is_ignored(self):
        self._run(self._event(".db   ", msg_id=11))
        self.assertEqual(self.stream_calls, [])

    def test_cooldown_blocks_second_message(self):
        userbot.COOLDOWN = 60
        self._run(self._event(".db первый", msg_id=12))
        self._run(self._event(".db второй", msg_id=13))
        self.assertEqual(len(self.stream_calls), 1)

    def test_own_message_edits_itself(self):
        self._run(self._event(".db моё сообщение", msg_id=14, out=True))
        self.assertEqual(self.stream_calls[0]["self_edit_id"], 14)
        self.assertEqual(self.stream_calls[0]["is_self"], True)
        self.assertIn("моё сообщение", self.stream_calls[0]["prefix"])

    def test_model_override_is_used(self):
        userbot.model_overrides[self.DM] = "custom-model"
        self._run(self._event(".db привет", msg_id=15))
        self.assertEqual(self.stream_calls[0]["model"], "custom-model")

    def test_history_is_appended_and_saved(self):
        self._run(self._event(".db привет", msg_id=16))
        entries = list(userbot.chat_history[self.DM])
        self.assertEqual(entries[0]["role"], "user")
        self.assertEqual(entries[-1]["role"], "assistant")
        self.assertGreaterEqual(self.saver.dirty, 1)

    def test_group_history_is_saved(self):
        self._run(self._event(".db привет", chat_id=self.GROUP, is_private=False))
        self.assertTrue(userbot.chat_history[self.GROUP])
        self.assertGreaterEqual(self.saver.dirty, 1)

    def test_stream_error_is_reported(self):
        async def failing(*_args, **_kwargs):
            raise userbot.OpenAIError("boom")

        saved = core.stream_answer
        self.addCleanup(setattr, core, "stream_answer", saved)
        core.stream_answer = failing
        event = self._event(".db привет", msg_id=17)
        self._run(event)
        self.assertIn("Ошибка", event.sent[0])

    def test_coder_command_points_to_bot(self):
        event = self._event(".db coder on", msg_id=18)
        self._run(event)
        self.assertIn("боте", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_coder_status_points_to_bot(self):
        event = self._event(".db coder", msg_id=19)
        self._run(event)
        self.assertIn("боте", event.sent[0])

    def test_prompt_command_is_owner_only(self):
        event = self._event(".db prompt", msg_id=20, sender_id=9)
        self._run(event)
        self.assertIn("только владельцу", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_prompt_command_for_owner(self):
        event = self._event(".db prompt", msg_id=21, sender_id=self.OWNER)
        self._run(event)
        self.assertIn("Режим / Mode", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_settings_command_is_owner_only(self):
        event = self._event(".db settings", msg_id=22, sender_id=9)
        self._run(event)
        self.assertIn("только владельцу", event.sent[0])

    def test_settings_command_for_owner(self):
        event = self._event(".db settings", msg_id=23, sender_id=self.OWNER)
        self._run(event)
        self.assertIn("Настройки", event.sent[0])
        self.assertEqual(self.stream_calls, [])

    def test_visibility_command_is_owner_only(self):
        event = self._event(".db reasoning off", msg_id=24, sender_id=9)
        self._run(event)
        self.assertIn("только владельцу", event.sent[0])
        self.assertNotIn(self.DM, userbot.reasoning_hidden)

    def test_visibility_command_toggles_state(self):
        event = self._event(".db reasoning off", msg_id=25, sender_id=self.OWNER)
        self._run(event)
        self.assertIn(self.DM, userbot.reasoning_hidden)
        status = self._event(".db reasoning", msg_id=26, sender_id=self.OWNER)
        self._run(status)
        self.assertIn("скрыты", status.sent[0])
        show = self._event(".db tools on", msg_id=27, sender_id=self.OWNER)
        self._run(show)
        self.assertIn("показаны", show.sent[0])

    def test_model_command_sets_and_shows(self):
        saved_models = userbot.MODELS
        self.addCleanup(setattr, userbot, "MODELS", saved_models)
        userbot.MODELS = ["my-model"]
        set_event = self._event(".db model my-model", msg_id=28, sender_id=self.OWNER)
        self._run(set_event)
        self.assertIn("my-model", set_event.sent[0])
        self.assertEqual(userbot.model_overrides[self.DM], "my-model")
        show = self._event(".db model", msg_id=29, sender_id=self.OWNER)
        self._run(show)
        self.assertIn("my-model", show.sent[0])

    def test_model_command_rejects_unknown_model(self):
        saved_models = userbot.MODELS
        self.addCleanup(setattr, userbot, "MODELS", saved_models)
        userbot.MODELS = ["known-model"]
        event = self._event(".db model nope", msg_id=35, sender_id=self.OWNER)
        self._run(event)
        self.assertIn("Доступные модели", event.sent[0])
        self.assertEqual(userbot.model_overrides.get(self.DM), None)

    def test_model_command_rejected_for_stranger(self):
        event = self._event(".db model evil", msg_id=33)
        self._run(event)
        self.assertIn("владельцу", event.sent[0])
        self.assertEqual(userbot.model_overrides.get(self.DM), None)

    def test_clear_command_rejected_for_stranger(self):
        userbot.chat_history[self.DM] = deque([{"role": "user", "content": "x"}])
        event = self._event(".db clear", msg_id=34)
        self._run(event)
        self.assertIn("владельцу", event.sent[0])
        self.assertEqual(len(userbot.chat_history[self.DM]), 1)

    def test_models_command_lists_models(self):
        event = self._event(".db models", msg_id=30, sender_id=self.OWNER)
        self._run(event)
        self.assertIn("Доступные модели", event.sent[0])

    def test_clear_command_resets_history(self):
        userbot.chat_history[self.DM] = deque([{"role": "user", "content": "x"}])
        event = self._event(".db clear", msg_id=31, sender_id=self.OWNER)
        self._run(event)
        self.assertEqual(len(userbot.chat_history[self.DM]), 0)
        self.assertIn("Контекст очищен", event.sent[0])

    def test_help_command_lists_aliases(self):
        event = self._event(".db help", msg_id=32)
        self._run(event)
        self.assertIn("Trigger aliases", event.sent[0])

    def test_unknown_command_falls_through_to_prompt(self):
        self._run(
            self._event(
                ".db неизвестная команда",
                msg_id=33,
                chat_id=self.GROUP,
                is_private=False,
            )
        )
        self.assertEqual(len(self.stream_calls), 1)
        self.assertIn("неизвестная команда", self._group_prompt())

    def test_bad_command_payload_is_ignored(self):
        def boom(_text):
            raise TypeError("bad command")

        saved = userbot.handle_commands
        userbot.handle_commands = boom
        self.addCleanup(setattr, userbot, "handle_commands", saved)
        self._run(self._event(".db привет", msg_id=50))
        self.assertEqual(len(self.stream_calls), 1)

    def test_cancellation_of_own_task_is_reraised(self):
        started = asyncio.Event()

        async def slow(*_args, **_kwargs):
            started.set()
            await asyncio.sleep(5)
            return "долгий ответ"

        core.stream_answer = slow
        self.addCleanup(userbot.SESSIONS.cancel_all)

        async def scenario():
            task = asyncio.ensure_future(
                userbot.handler(self._event(".db привет", chat_id=4242))
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        record = userbot.TASKS.get(4242) or {}
        self.assertEqual(record.get("status"), core.TASK_INTERRUPTED)


class HardeningTest(BotTestCase):
    def _state(self, model="m"):
        return {
            "model_overrides": {1: model},
            "coder_chats": set(),
            "reasoning_hidden": set(),
            "tools_hidden": set(),
        }

    def _tmp_dir(self, prefix):
        tmp = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp

    def _use_coder_root(self, path):
        saved = tools_module.CODER_ROOT
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved)
        tools_module.CODER_ROOT = path

    def test_render_keeps_answer_with_long_prefix(self):
        text = tools_module.render_response(
            "я" * 4000 + "\n\nmodel:\n\n", [], [], "ОТВЕТ " * 400
        )
        self.assertIn("ОТВЕТ", text)
        self.assertLessEqual(len(text), tools_module.MAX_RENDER_CHARS)

    def test_render_drops_reasoning_before_answer(self):
        text = tools_module.render_response(
            "prefix:\n\n", ["р" * 3000], ["run_shell"], "ОТВЕТ " * 500
        )
        self.assertIn("ОТВЕТ", text)
        self.assertIn("tools: run_shell", text)
        self.assertNotIn("р" * 100, text)
        self.assertLessEqual(len(text), tools_module.MAX_RENDER_CHARS)

    def test_read_and_edit_file_reject_huge_files(self):
        tmp = self._tmp_dir("danybot_big_")
        (tmp / "big.txt").write_text("x" * 500, encoding="utf-8")
        for attr, value in (
            ("CODER_ROOT", tmp),
            ("MAX_READ_BYTES", 100),
        ):
            saved = getattr(tools_module, attr)
            self.addCleanup(setattr, tools_module, attr, saved)
            setattr(tools_module, attr, value)
        read = _owner_tool("read_file", {"path": "big.txt"}, 1)
        self.assertIn("слишком большой", read)
        edit = _owner_tool(
            "edit_file",
            {"path": "big.txt", "old_string": "x", "new_string": "y"},
            1,
        )
        self.assertIn("слишком большой", edit)

    def test_read_file_works_within_limit(self):
        tmp = self._tmp_dir("danybot_ok_")
        (tmp / "a.txt").write_text("one\ntwo\nthree", encoding="utf-8")
        saved = tools_module.CODER_ROOT
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved)
        tools_module.CODER_ROOT = tmp
        out = _owner_tool("read_file", {"path": "a.txt", "offset": 2}, 1)
        self.assertIn("2|two", out)
        self.assertIn("3|three", out)

    def test_fetch_url_marks_truncated_download(self):
        payload = "<html><body>" + ("a" * 5000) + "</body></html>"
        with (
            mock.patch.object(
                tools_module, "_get_httpx_client", lambda: _FakeHttpx(payload)
            ),
            mock.patch.object(tools_module, "MAX_FETCH_BYTES", 1024),
        ):
            out = asyncio.run(
                tools_module.execute_tool(
                    "fetch_url", {"url": "https://93.184.216.34/"}, 1
                )
            )
        self.assertIn("загрузка обрезана", out)
        self.assertLess(len(out), 1200)

    def test_execute_script_cleans_temp_file_on_launch_failure(self):
        tmp = self._tmp_dir("danybot_tmpdir_")
        saved_tempdir = tempfile.tempdir
        self.addCleanup(setattr, tempfile, "tempdir", saved_tempdir)
        tempfile.tempdir = str(tmp)

        async def broken(*_args, **_kwargs):
            raise ValueError("no interpreter")

        saved_exec = asyncio.create_subprocess_exec
        self.addCleanup(setattr, asyncio, "create_subprocess_exec", saved_exec)
        asyncio.create_subprocess_exec = broken
        out = _owner_tool("execute_script", {"code": "print(1)"}, 1)
        self.assertIn("Ошибка инструмента", out)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_execute_script_cleans_temp_file_when_write_fails(self):
        tmp = self._tmp_dir("danybot_tmpdir2_")
        saved_tempdir = tempfile.tempdir
        self.addCleanup(setattr, tempfile, "tempdir", saved_tempdir)
        tempfile.tempdir = str(tmp)
        real = tempfile.NamedTemporaryFile

        def failing(*args, **kwargs):
            handle = real(*args, **kwargs)
            handle.write = _raise_oserror
            return handle

        self.addCleanup(setattr, tempfile, "NamedTemporaryFile", real)
        tempfile.NamedTemporaryFile = failing
        out = _owner_tool("execute_script", {"code": "print(1)"}, 1)
        self.assertIn("Ошибка", out)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_write_file_keeps_original_when_write_fails(self):
        tmp = self._tmp_dir("danybot_atomic_")
        target = tmp / "keep.txt"
        target.write_text("ORIGINAL", encoding="utf-8")
        saved_root = tools_module.CODER_ROOT
        saved_write = tools_module.core._write_text_atomic
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved_root)
        self.addCleanup(setattr, tools_module.core, "_write_text_atomic", saved_write)
        tools_module.CODER_ROOT = tmp
        tools_module.core._write_text_atomic = lambda *_a, **_k: False
        out = _owner_tool("write_file", {"path": "keep.txt", "content": "LOST"}, 1)
        self.assertIn("Ошибка записи", out)
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL")

    def test_edit_file_keeps_original_when_write_fails(self):
        tmp = self._tmp_dir("danybot_atomic2_")
        target = tmp / "keep.txt"
        target.write_text("ORIGINAL", encoding="utf-8")
        saved_root = tools_module.CODER_ROOT
        saved_write = tools_module.core._write_text_atomic
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved_root)
        self.addCleanup(setattr, tools_module.core, "_write_text_atomic", saved_write)
        tools_module.CODER_ROOT = tmp
        tools_module.core._write_text_atomic = lambda *_a, **_k: False
        out = _owner_tool(
            "edit_file",
            {"path": "keep.txt", "old_string": "ORIGINAL", "new_string": "NEW"},
            1,
        )
        self.assertIn("Ошибка записи", out)
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL")

    def test_edit_file_rejects_oversized_result(self):
        tmp = self._tmp_dir("danybot_cap_")
        target = tmp / "a.txt"
        target.write_text("tiny", encoding="utf-8")
        saved_root = tools_module.CODER_ROOT
        saved_cap = tools_module.MAX_WRITE_BYTES
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved_root)
        self.addCleanup(setattr, tools_module, "MAX_WRITE_BYTES", saved_cap)
        tools_module.CODER_ROOT = tmp
        tools_module.MAX_WRITE_BYTES = 64
        out = _owner_tool(
            "edit_file",
            {"path": "a.txt", "old_string": "tiny", "new_string": "x" * 500},
            1,
        )
        self.assertIn("Слишком большой", out)
        self.assertEqual(target.read_text(encoding="utf-8"), "tiny")

    def test_render_keeps_answer_when_only_answer_fits(self):
        text = tools_module.render_response(
            "p" * 800 + "\n\n", ["р" * 3000], ["a", "b", "c"], "ОТВЕТ"
        )
        self.assertIn("ОТВЕТ", text)
        self.assertLessEqual(len(text), tools_module.MAX_RENDER_CHARS)

    def test_strip_page_drops_script_and_style_keeps_text(self):
        raw = (
            "<html><head><style>a{b}</style></head><body><p>Hello</p>"
            "<script>var x=1;</script><p>World</p></body></html>"
        )
        self.assertEqual(tools_module._strip_page(raw), "Hello World")

    def test_strip_page_survives_pathological_input(self):
        raw = "<scriptaaaa" * 20000
        started = time.monotonic()
        out = tools_module._strip_page(raw)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertLess(len(out), len(raw))

    def test_strip_page_keeps_text_before_unterminated_tag(self):
        raw = "BEFORE " + "z" * 1990 + " < " + "q" * 5000 + " AFTER"
        out = tools_module._strip_page(raw)
        self.assertTrue(out.startswith("BEFORE"))
        self.assertIn("AFTER", out)

    def test_strip_page_keeps_text_between_blocks(self):
        self.assertEqual(
            tools_module._strip_page("<style>.a{}</style>mid<style>.b{}</style>end"),
            "mid end",
        )

    def test_output_exactly_at_cap_is_not_marked_truncated(self):
        sink = tools_module._OutputSink(10)
        sink.feed(b"0123456789")
        self.assertFalse(sink.truncated)
        self.assertEqual(sink.text(), "0123456789")

    def test_output_over_cap_is_marked_truncated(self):
        sink = tools_module._OutputSink(10)
        sink.feed(b"0123456789A")
        self.assertTrue(sink.truncated)
        self.assertEqual(sink.text(), "0123456789")

    def test_interrupted_shell_child_does_not_outlive_the_task(self):
        tmp = self._tmp_dir("danybot_kill_")
        self._use_coder_root(tmp)
        marker = tmp / "survived.txt"
        script = (
            "import time,pathlib;"
            f"time.sleep(1);pathlib.Path({str(marker)!r}).write_text('x')"
        )
        command = f'"{sys.executable}" -c "{script}"'

        async def scenario():
            task = asyncio.ensure_future(
                userbot.execute_tool(
                    "run_shell",
                    {"command": command, "cwd": str(tmp)},
                    1,
                    unrestricted=True,
                )
            )
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        time.sleep(1.6)
        self.assertFalse(marker.exists(), "процесс пережил прерывание задачи")

    def test_shell_timeout_kills_grandchildren(self):
        tmp = self._tmp_dir("danybot_kill2_")
        self._use_coder_root(tmp)
        marker = tmp / "survived_timeout.txt"
        script = (
            "import time,pathlib;"
            f"time.sleep(1.2);pathlib.Path({str(marker)!r}).write_text('x')"
        )
        command = f'"{sys.executable}" -c "{script}"'
        out = asyncio.run(
            userbot.execute_tool(
                "run_shell",
                {"command": command, "cwd": str(tmp), "timeout": 1},
                1,
                unrestricted=True,
            )
        )
        self.assertIn("Таймаут 1s", out)
        time.sleep(1.7)
        self.assertFalse(marker.exists(), "процесс пережил таймаут")

    def test_scan_files_stops_on_directory_flood(self):
        tmp = self._tmp_dir("danybot_scan_")
        deep = tmp
        for i in range(300):
            deep = deep / f"d{i}"
            deep.mkdir()
        (deep / "hit.txt").write_text("needle", encoding="utf-8")
        self._use_coder_root(tmp)
        saved = tools_module.MAX_SEARCH_NODES
        self.addCleanup(setattr, tools_module, "MAX_SEARCH_NODES", saved)
        tools_module.MAX_SEARCH_NODES = 10
        out = _owner_tool("search_files", {"pattern": "needle"}, 1)
        self.assertIn("Совпадений не найдено", out)
        self.assertIn("Поиск неполный", out)
        self.assertIn("элементов", out)

    def test_scan_files_reports_huge_files_and_result_cap(self):
        tmp = self._tmp_dir("danybot_scan3_")
        (tmp / "big.txt").write_text("needle " + "x" * 100, encoding="utf-8")
        (tmp / "a_first.txt").write_text("needle", encoding="utf-8")
        (tmp / "z_last.txt").write_text("needle", encoding="utf-8")
        self._use_coder_root(tmp)
        saved = tools_module.MAX_SEARCH_FILE_BYTES
        self.addCleanup(setattr, tools_module, "MAX_SEARCH_FILE_BYTES", saved)
        tools_module.MAX_SEARCH_FILE_BYTES = 10
        out = _owner_tool("search_files", {"pattern": "needle"}, 1)
        self.assertIn("a_first.txt", out)
        self.assertIn("z_last.txt", out)
        self.assertNotIn("big.txt", out)
        self.assertIn("Поиск неполный", out)
        self.assertIn("байт", out)
        capped = _owner_tool("search_files", {"pattern": "needle", "limit": 1}, 1)
        self.assertIn("a_first.txt", capped)
        self.assertNotIn("z_last.txt", capped)
        self.assertIn("Поиск неполный", capped)
        self.assertIn("результатов", capped)

    def test_scan_files_ignores_files_outside_root(self):
        tmp = self._tmp_dir("danybot_scan4_")
        root = tmp / "root"
        root.mkdir()
        (tmp / "outside.txt").write_text("needle", encoding="utf-8")
        self._use_coder_root(root)
        out = _owner_tool("search_files", {"pattern": "needle"}, 1)
        self.assertIn("Совпадений не найдено", out)
        self.assertNotIn("Поиск неполный", out)

    def test_scan_files_finds_match_within_node_budget(self):
        tmp = self._tmp_dir("danybot_scan2_")
        (tmp / "hit.txt").write_text("needle", encoding="utf-8")
        saved = tools_module.CODER_ROOT
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved)
        tools_module.CODER_ROOT = tmp
        out = _owner_tool("search_files", {"pattern": "needle"}, 1)
        self.assertIn("hit.txt", out)

    def test_web_search_error_hides_query(self):
        class _Boom:
            def get(self, *_args, **kwargs):
                raise httpx.ConnectError("failed for https://x.test/?q=secret-token")

        with (
            mock.patch.object(tools_module, "_get_httpx_client", lambda: _Boom()),
            self.assertLogs(tools_module.logger, "WARNING") as captured,
        ):
            out = asyncio.run(
                tools_module.execute_tool("web_search", {"query": "secret-token"}, 1)
            )
        joined = "\n".join(captured.output)
        self.assertNotIn("secret-token", joined)
        self.assertNotIn("secret-token", out)

    def test_async_saver_keeps_dirty_when_write_is_cancelled(self):
        saver = core.AsyncSaver(lambda: None, delay=0.01)
        dirty = []

        async def run():
            async def cancelled_write():
                raise asyncio.CancelledError

            saver._write = cancelled_write
            saver.mark_dirty()
            await asyncio.sleep(0.05)
            dirty.append(saver._dirty)
            task = saver._task
            self.assertIsNotNone(task)
            self.assertTrue(cast(Any, task).done())

        asyncio.run(run())
        self.assertEqual(dirty, [True])

    def test_async_saver_flush_waits_for_inflight_write(self):
        calls = []
        saver = core.AsyncSaver(lambda: calls.append(1), delay=0.05)

        async def run():
            saver.mark_dirty()
            await saver.flush()

        asyncio.run(run())
        self.assertEqual(calls, [1])
        self.assertFalse(saver._dirty)

    def test_async_saver_flush_writes_without_task(self):
        calls = []
        saver = core.AsyncSaver(lambda: calls.append(1), delay=0.01)

        async def run():
            saver._dirty = True
            saver._task = None
            await saver.flush()

        asyncio.run(run())
        self.assertEqual(calls, [1])

    def test_atomic_write_survives_parallel_writers(self):
        tmp = self._tmp_dir("danybot_parallel_")
        target = tmp / "state.json"
        self.assertTrue(core.save_state_file(target, self._state()))
        failed = []

        def writer(index):
            for _ in range(20):
                if not core.save_state_file(target, self._state(f"m{index}")):
                    failed.append(index)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(writer, range(8)))
        self.assertEqual(failed, [])
        data = json.loads(target.read_text(encoding="utf-8"))
        written = next(iter(data["model_overrides"].values()))
        self.assertIn(written, {f"m{index}" for index in range(8)})
        self.assertEqual([p.name for p in tmp.iterdir()], ["state.json"])

    def test_history_save_survives_concurrent_growth(self):
        tmp = self._tmp_dir("danybot_hgrow_")
        target = tmp / "history.json"
        history: dict[int, deque] = {1: deque(maxlen=5)}
        failed = []
        stop = False

        def writer():
            while not stop:
                if not core.save_history_file(target, history):
                    failed.append(1)

        def grower():
            for index in range(400):
                history[index + 2] = deque(
                    [{"role": "user", "content": str(index)}], maxlen=5
                )

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(writer) for _ in range(3)]
            pool.submit(grower).result()
            stop = True
            for item in futures:
                item.result()
        self.assertEqual(failed, [])
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertEqual([p.name for p in tmp.iterdir()], ["history.json"])

    def test_build_payload_retries_on_runtime_error(self):
        calls = {"n": 0}

        def builder():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("dictionary changed size during iteration")
            return "payload"

        self.assertEqual(core._build_payload(builder), "payload")
        self.assertEqual(calls["n"], 3)

    def test_build_payload_gives_up_after_retries(self):
        def builder():
            raise RuntimeError("dictionary changed size during iteration")

        with self.assertRaises(RuntimeError):
            core._build_payload(builder)

    def test_write_text_atomic_accepts_builder(self):
        tmp = self._tmp_dir("danybot_builder_")
        target = tmp / "out.txt"
        calls = {"n": 0}

        def builder():
            calls["n"] += 1
            return f"value-{calls['n']}"

        self.assertTrue(core._write_text_atomic(target, builder))
        self.assertEqual(target.read_text(encoding="utf-8"), "value-1")

    def test_write_text_atomic_reports_builder_failure(self):
        tmp = self._tmp_dir("danybot_builder2_")
        target = tmp / "out.txt"

        def builder():
            raise OSError("disk full")

        self.assertFalse(core._write_text_atomic(target, builder))
        self.assertEqual(list(tmp.iterdir()), [])

    def test_trim_tool_history_keeps_tool_pairs(self):
        messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
        for index in range(5):
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": str(index), "type": "function", "function": {}}
                    ],
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": str(index), "content": "r"}
            )
        self.assertIs(core.trim_tool_history(messages, 100), messages)
        trimmed = core.trim_tool_history(messages, 5)
        self.assertEqual(trimmed[0]["role"], "system")
        self.assertEqual(trimmed[1]["role"], "assistant")
        self.assertLessEqual(len(trimmed), 5)
        self.assertEqual(trimmed[-1]["role"], "tool")
        self.assertIs(core.trim_tool_history(messages, 1), messages)

    def test_run_subagent_tool_returns_valid_json(self):
        async def fake_run(tasks, **kwargs):
            self.assertEqual(tasks, ["task one"])
            return [
                {
                    "name": "a",
                    "task": "task one",
                    "ok": True,
                    "rounds": 2,
                    "tools_used": ["evaluate"],
                    "result": "x" * 9000,
                }
            ]

        for attr, value in (
            ("run_subagents", fake_run),
            ("is_configured", lambda: True),
        ):
            saved = getattr(subagents, attr)
            self.addCleanup(setattr, subagents, attr, saved)
            setattr(subagents, attr, value)
        out = _owner_tool("run_subagent", {"task": "task one"}, 1)
        data = json.loads(out)
        self.assertTrue(data[0]["ok"])
        self.assertEqual(len(data[0]["result"]), tools_module.SUBAGENT_RESULT_CHARS)

    def test_run_subagent_tool_marks_truncated_report(self):
        async def fake_run(tasks, **kwargs):
            return [
                {
                    "name": f"s{index}",
                    "task": "t",
                    "ok": True,
                    "rounds": 1,
                    "tools_used": [],
                    "result": "y" * 2000,
                }
                for index in range(10)
            ]

        for attr, value in (
            ("run_subagents", fake_run),
            ("is_configured", lambda: True),
        ):
            saved = getattr(subagents, attr)
            self.addCleanup(setattr, subagents, attr, saved)
            setattr(subagents, attr, value)
        out = _owner_tool("run_subagent", {"task": "t"}, 1)
        self.assertLessEqual(len(out), tools_module.SUBAGENT_REPORT_CHARS)
        self.assertEqual(len(json.loads(out)), 10)

    def test_subagent_report_stays_valid_json_when_shrunk(self):
        results = [
            {
                "name": "n" * 200,
                "task": "t" * 2000,
                "ok": True,
                "rounds": 2,
                "tools_used": ["x"],
                "result": "y" * 5000,
            }
            for _ in range(16)
        ]
        out = tools_module._subagent_report(results)
        self.assertLessEqual(len(out), tools_module.SUBAGENT_REPORT_CHARS)
        parsed = json.loads(out)
        self.assertEqual(len(parsed), 16)
        for item in parsed:
            self.assertLessEqual(len(item["name"]), tools_module.SUBAGENT_NAME_CHARS)
            self.assertLessEqual(len(item["task"]), tools_module.SUBAGENT_TASK_CHARS)

    def test_owner_only_tools_are_denied_for_strangers(self):
        for name in sorted(tools_module.OWNER_ONLY_TOOLS):
            with self.subTest(tool=name):
                out = asyncio.run(
                    tools_module.execute_tool(name, {"command": "echo x"}, 1)
                )
                self.assertIn("только владельцу", out)
                out_owner = _owner_tool(name, {}, 1)
                self.assertNotIn("только владельцу", out_owner)

    def test_public_tools_menu_excludes_owner_only(self):
        public = tools_module.tool_names_of(tools_module.PUBLIC_TOOLS)
        self.assertTrue(public)
        self.assertEqual(public & tools_module.OWNER_ONLY_TOOLS, set())
        for name in ("evaluate", "web_search", "fetch_url", "memory_recall"):
            self.assertIn(name, public)
        owner_menus = tools_module.tool_names_of(
            [*tools_module.CODER_TOOLS, *tools_module.BOT_TOOLS]
        )
        for name in tools_module.OWNER_ONLY_TOOLS:
            self.assertIn(name, owner_menus)

    def test_subprocess_env_drops_secrets(self):
        for key, value in (
            ("BOT_TOKEN", "123:secret"),
            ("DANYAPI_KEY", "sk-secret"),
            ("API_HASH", "hash-secret"),
            ("SESSION_NAME", "1secret-session"),
            ("GH_TOKEN", "ghp-secret"),
            ("MY_PASSWORD", "hunter2"),
        ):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        os.environ["KEEP_ME"] = "visible"
        self.addCleanup(os.environ.pop, "KEEP_ME", None)
        os.environ["OWNER_IDS"] = "5,7"
        self.addCleanup(os.environ.pop, "OWNER_IDS", None)
        env = tools_module._subprocess_env()
        self.assertNotIn("BOT_TOKEN", env)
        self.assertNotIn("DANYAPI_KEY", env)
        self.assertNotIn("API_HASH", env)
        self.assertNotIn("SESSION_NAME", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("MY_PASSWORD", env)
        self.assertNotIn("OWNER_IDS", env)
        self.assertEqual(env["KEEP_ME"], "visible")
        self.assertIn("PATH", env)

    def test_search_scanner_does_not_see_secrets(self):
        tmp = self._tmp_dir("danybot_scanenv_")
        (tmp / "note.txt").write_text("секрет=abc\n", encoding="utf-8")
        for key, value in (
            ("DANYBOT_CANARY_TOKEN", "scanner-canary-77"),
            ("BOT_TOKEN", f"{AUTH_PART}:scan-secret"),
        ):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        probe = tmp / "probe.py"
        probe.write_text(
            "import json, os, sys\n"
            f"sys.path.insert(0, r'{PROJECT_DIR}')\n"
            "import tools\n"
            "tools._scan_worker_main()\n"
            "sys.stderr.write(json.dumps({k: os.environ.get(k, '') for k in "
            "('BOT_TOKEN', 'DANYBOT_CANARY_TOKEN')}))\n",
            encoding="utf-8",
        )
        env = tools_module._subprocess_env()
        env["PYTHONPATH"] = str(PROJECT_DIR)
        request = json.dumps(
            {
                "root": str(tmp),
                "glob": "*.txt",
                "pattern": "секрет",
                "limit": 5,
                "limits": {},
            }
        )
        proc = subprocess.run(
            [sys.executable, str(probe)],
            input=request.encode("utf-8"),
            capture_output=True,
            env=env,
            check=False,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(payload["error"], "")
        self.assertTrue(payload["matches"])
        seen = json.loads(
            proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1]
        )
        self.assertEqual(seen["BOT_TOKEN"], "")
        self.assertEqual(seen["DANYBOT_CANARY_TOKEN"], "")

    def test_run_shell_child_does_not_see_secrets(self):
        marker = "canary-value-42"
        for key, value in (("DANYBOT_CANARY_TOKEN", marker),):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        out = _owner_tool(
            "run_shell",
            {
                "command": (
                    f'"{sys.executable}" -c "import os;'
                    "print(os.environ.get('DANYBOT_CANARY_TOKEN', 'absent'))\""
                )
            },
            1,
        )
        self.assertIn("absent", out)
        self.assertNotIn(marker, out)

    def test_search_files_glob_cannot_escape_root(self):
        tmp = self._tmp_dir("danybot_glob_")
        root = tmp / "root"
        root.mkdir()
        (root / "inside.txt").write_text("needle-inside", encoding="utf-8")
        (tmp / "outside.txt").write_text("needle-outside", encoding="utf-8")
        saved_root = tools_module.CODER_ROOT
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved_root)
        tools_module.CODER_ROOT = root
        for pattern in ("../outside.txt", "..\\outside.txt", "/etc/passwd", "~/x"):
            with self.subTest(glob=pattern):
                out = _owner_tool(
                    "search_files",
                    {"pattern": "needle", "path": ".", "glob": pattern},
                    1,
                )
                self.assertIn("Недопустимый glob", out)
        out = _owner_tool(
            "search_files",
            {"pattern": "needle", "path": ".", "glob": "../*.txt"},
            1,
        )
        self.assertIn("Недопустимый glob", out)

    def test_search_files_skips_symlink_out_of_root(self):
        tmp = self._tmp_dir("danybot_link_")
        root = tmp / "root"
        root.mkdir()
        (tmp / "secret.txt").write_text("needle-secret", encoding="utf-8")
        link = root / "link.txt"
        try:
            link.symlink_to(tmp / "secret.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        (root / "ok.txt").write_text("needle-ok", encoding="utf-8")
        saved_root = tools_module.CODER_ROOT
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved_root)
        tools_module.CODER_ROOT = root
        out = _owner_tool("search_files", {"pattern": "needle", "path": "."}, 1)
        self.assertIn("ok.txt", out)
        self.assertNotIn("secret.txt", out)
        self.assertNotIn("needle-secret", out)

    def test_list_dir_counts_all_entries_but_caps_output(self):
        tmp = self._tmp_dir("danybot_listdir_")
        for index in range(5):
            (tmp / f"f{index}.txt").write_text("x", encoding="utf-8")
        saved_root = tools_module.CODER_ROOT
        saved_cap = tools_module.MAX_LIST_ENTRIES
        self.addCleanup(setattr, tools_module, "CODER_ROOT", saved_root)
        self.addCleanup(setattr, tools_module, "MAX_LIST_ENTRIES", saved_cap)
        tools_module.CODER_ROOT = tmp
        tools_module.MAX_LIST_ENTRIES = 2
        out = _owner_tool("list_dir", {"path": "."}, 1)
        self.assertIn("элементов 5", out)
        self.assertIn("показано 2", out)
        self.assertNotIn("f4.txt", out)
        entries, total = tools_module._list_entries(tmp)
        self.assertEqual(total, 5)
        self.assertEqual(len(entries), 2)

    def test_safe_eval_rejects_keyword_arguments(self):
        out = tools_module.safe_eval("round(2.7, ndigits=0)")
        self.assertIn("Именованные аргументы", out)
        self.assertEqual(tools_module.safe_eval("round(2.7)"), "3")

    def test_fetch_url_rejects_non_http_redirect(self):
        class _RedirectClient:
            def stream(self, _method, url, **_kwargs):
                return _FakeStream(_FakeResp("", 302, location="file:///etc/passwd"))

        with mock.patch.object(
            tools_module, "_get_httpx_client", lambda: _RedirectClient()
        ):
            out = asyncio.run(
                tools_module.execute_tool(
                    "fetch_url", {"url": "https://93.184.216.34/"}, 1
                )
            )
        self.assertIn("Схема заблокирована", out)

    def test_web_search_reports_engine_failure_over_empty_result(self):
        class _MixedClient:
            def __init__(self):
                self.calls = 0

            def stream(self, _method, url, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _FakeStream(_FakeResp("", 500))
                return _FakeStream(_FakeResp("<html></html>"))

        with mock.patch.object(
            tools_module, "_get_httpx_client", lambda: _MixedClient()
        ):
            out = asyncio.run(
                tools_module.execute_tool("web_search", {"query": "x"}, 1)
            )
        self.assertIn("Ошибка поиска", out)
        self.assertNotEqual(out, "Ничего не найдено.")

    def test_run_subagent_accepts_tools_as_string(self):
        seen = {}

        async def fake_run(tasks, **kwargs):
            seen["tools"] = kwargs.get("tool_names")
            return []

        for attr, value in (
            ("run_subagents", fake_run),
            ("is_configured", lambda: True),
        ):
            saved = getattr(subagents, attr)
            self.addCleanup(setattr, subagents, attr, saved)
            setattr(subagents, attr, value)
        _owner_tool("run_subagent", {"task": "t", "tools": "evaluate"}, 1)
        self.assertEqual(seen["tools"], ["evaluate"])
        _owner_tool("run_subagent", {"task": "t", "tools": []}, 1)
        self.assertIsNone(seen["tools"])

    def test_run_subagent_max_rounds_is_accepted(self):
        seen = {}

        async def fake_run(tasks, **kwargs):
            seen["max_rounds"] = kwargs.get("max_rounds")
            return []

        for attr, value in (
            ("run_subagents", fake_run),
            ("is_configured", lambda: True),
        ):
            saved = getattr(subagents, attr)
            self.addCleanup(setattr, subagents, attr, saved)
            setattr(subagents, attr, value)
        out = _owner_tool("run_subagent", {"task": "t", "max_rounds": 3}, 1)
        self.assertEqual(seen["max_rounds"], 3)
        self.assertNotIn("Ошибка инструмента", out)
        _owner_tool("run_subagent", {"task": "t"}, 1)
        self.assertIsNone(seen["max_rounds"])
        _owner_tool("run_subagent", {"task": "t", "max_rounds": "junk"}, 1)
        self.assertEqual(seen["max_rounds"], 1)

    def test_drain_timeout_defaults_and_override(self):
        self.assertEqual(core.DRAIN_TIMEOUT, 5.0)
        registry = core.SessionRegistry()
        held = []

        async def scenario():
            async def slow():
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.05)
                    held.append(1)
                    raise

            registry.start(1, slow(), scope="owner")
            await asyncio.sleep(0)
            registry.cancel_all()
            await registry.drain(1)

        asyncio.run(scenario())
        self.assertEqual(held, [1])

    def test_get_time_weekday_is_deterministic(self):
        import datetime

        out = json.loads(asyncio.run(tools_module.execute_tool("get_time", {}, 1)))
        expected = tools_module.WEEKDAYS[
            datetime.datetime.fromisoformat(out["utc"]).weekday()
        ]
        self.assertEqual(out["weekday"], expected)

    def test_evict_oldest_keeps_most_entries(self):
        target = set(range(10))
        core._evict_oldest(target, 5)
        self.assertEqual(len(target), 5)
        core._evict_oldest(target, 50)
        self.assertEqual(len(target), 5)

    def test_compose_prompt_variants(self):
        self.assertEqual(core.compose_prompt("вопрос", None, 100), "вопрос")
        self.assertEqual(core.compose_prompt("", "цитата", 100), "цитата")
        merged = core.compose_prompt("вопрос", "цитата", 100)
        self.assertIn("Сообщение, на которое ответили", merged)
        self.assertTrue(merged.endswith("вопрос"))
        long = core.compose_prompt("x" * 500, "y" * 500, 100)
        self.assertLessEqual(len(long), 200)
        for limit in (10, 40, 80, 200):
            with self.subTest(limit=limit):
                self.assertLessEqual(
                    len(core.compose_prompt("x" * 500, "y" * 500, limit)), limit
                )

    def test_fetch_replied_message_handles_errors(self):
        class _Boom:
            is_reply = True

            async def get_reply_message(self):
                raise OSError("boom")

        self.assertIsNone(asyncio.run(core.fetch_replied_message(_Boom())))
        self.assertIsNone(asyncio.run(core.fetch_replied_text(_Boom())))

        class _NoReply:
            is_reply = False

        self.assertIsNone(asyncio.run(core.fetch_replied_message(_NoReply())))
        self.assertIsNone(asyncio.run(core.fetch_replied_text(_NoReply())))

    def test_model_menu_reports_hidden_models(self):
        saved_models = list(userbot.MODELS)
        self.addCleanup(setattr, userbot, "MODELS", type(userbot.MODELS)(saved_models))
        userbot.MODELS = [f"model-{i}" for i in range(bot.MODEL_MENU_LIMIT + 3)]
        rows = bot._model_rows(1).inline_keyboard
        labels = [row[0].text for row in rows]
        self.assertTrue(any("Ещё 3" in label for label in labels))
        self.assertEqual(len(rows), bot.MODEL_MENU_LIMIT + 2)

    def test_model_menu_has_no_overflow_row_when_short(self):
        saved_models = list(userbot.MODELS)
        self.addCleanup(setattr, userbot, "MODELS", type(userbot.MODELS)(saved_models))
        userbot.MODELS = ["only-one"]
        rows = bot._model_rows(1).inline_keyboard
        self.assertEqual(len(rows), 2)

    def test_callback_models_action_lists_every_model(self):
        saved_models = list(userbot.MODELS)
        saved_owners = userbot.OWNER_IDS
        self.addCleanup(setattr, userbot, "MODELS", type(userbot.MODELS)(saved_models))
        self.addCleanup(setattr, userbot, "OWNER_IDS", saved_owners)
        userbot.OWNER_IDS = {5}
        userbot.MODELS = [f"model-{i}" for i in range(bot.MODEL_MENU_LIMIT + 2)]
        event = _CallbackEvent("settings:models", 5, 5)
        asyncio.run(bot.callback_handler(event))
        joined = "\n".join(r[0] for r in event.replies if r[0])
        self.assertIn(f"model-{bot.MODEL_MENU_LIMIT + 1}", joined)

    def test_refresh_models_survives_bad_payload(self):
        class _BadModels:
            data = None

        async def _list():
            return _BadModels()

        saved_ai = userbot.ai
        saved_models = userbot.MODELS
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        self.addCleanup(setattr, userbot, "MODELS", saved_models)
        userbot.ai = SimpleNamespace(models=SimpleNamespace(list=_list))
        asyncio.run(userbot.refresh_models())
        self.assertTrue(userbot.MODELS)

    def test_refresh_models_ignores_entries_without_id(self):
        class _Entry:
            def __init__(self, ident):
                self.id = ident

        class _Payload:
            data: ClassVar[list] = [_Entry("good"), _Entry(None), _Entry(5)]

        async def _list():
            return _Payload()

        saved_ai = userbot.ai
        saved_models = userbot.MODELS
        self.addCleanup(setattr, userbot, "ai", saved_ai)
        self.addCleanup(setattr, userbot, "MODELS", saved_models)
        userbot.ai = SimpleNamespace(models=SimpleNamespace(list=_list))
        asyncio.run(userbot.refresh_models())
        self.assertEqual(userbot.MODELS, ["good"])

    def test_read_file_reports_missing_path(self):
        tmp = self._tmp_dir("danybot_rf_")
        self._use_coder_root(tmp)
        out = _owner_tool("read_file", {"path": "нет.txt"}, 1)
        self.assertIn("Файл не найден", out)

    def test_read_file_reports_empty_window(self):
        tmp = self._tmp_dir("danybot_rf2_")
        (tmp / "a.txt").write_text("одна\nвторая", encoding="utf-8")
        self._use_coder_root(tmp)
        out = _owner_tool("read_file", {"path": "a.txt", "offset": 50}, 1)
        self.assertIn("(пусто)", out)

    def test_read_file_reports_read_error(self):
        tmp = self._tmp_dir("danybot_rf3_")
        (tmp / "a.txt").write_text("текст", encoding="utf-8")
        self._use_coder_root(tmp)
        with mock.patch.object(Path, "read_text", side_effect=OSError("сбой ввода")):
            out = _owner_tool("read_file", {"path": "a.txt"}, 1)
        self.assertIn("Ошибка чтения", out)

    def test_read_file_rejects_directory(self):
        tmp = self._tmp_dir("danybot_rf4_")
        (tmp / "sub").mkdir()
        self._use_coder_root(tmp)
        out = _owner_tool("read_file", {"path": "sub"}, 1)
        self.assertIn("Используй list_dir", out)

    def test_write_file_rejects_path_outside_root(self):
        out = _owner_tool("write_file", {"path": "/etc/hosts", "content": "x"}, 1)
        self.assertIn("вне разрешённого корня", out)

    def test_write_file_rejects_oversized_content(self):
        tmp = self._tmp_dir("danybot_wf_")
        self._use_coder_root(tmp)
        saved = tools_module.MAX_WRITE_BYTES
        self.addCleanup(setattr, tools_module, "MAX_WRITE_BYTES", saved)
        tools_module.MAX_WRITE_BYTES = 10
        out = _owner_tool("write_file", {"path": "a.txt", "content": "я" * 100}, 1)
        self.assertIn("Слишком большой объём", out)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_write_file_rejects_directory_target(self):
        tmp = self._tmp_dir("danybot_wf2_")
        (tmp / "sub").mkdir()
        self._use_coder_root(tmp)
        out = _owner_tool("write_file", {"path": "sub", "content": "x"}, 1)
        self.assertIn("Это каталог", out)

    def test_write_file_reports_mkdir_error(self):
        tmp = self._tmp_dir("danybot_wf3_")
        self._use_coder_root(tmp)
        with mock.patch.object(Path, "mkdir", side_effect=OSError("нет прав")):
            out = _owner_tool("write_file", {"path": "sub/a.txt", "content": "x"}, 1)
        self.assertIn("Ошибка записи", out)

    def test_write_file_reports_overwrite(self):
        tmp = self._tmp_dir("danybot_wf4_")
        self._use_coder_root(tmp)
        self.assertIn(
            "Создан", _owner_tool("write_file", {"path": "a.txt", "content": "x"}, 1)
        )
        self.assertIn(
            "Перезаписан",
            _owner_tool("write_file", {"path": "a.txt", "content": "y"}, 1),
        )

    def test_edit_file_rejects_path_outside_root(self):
        out = _owner_tool(
            "edit_file",
            {"path": "/etc/hosts", "old_string": "a", "new_string": "b"},
            1,
        )
        self.assertIn("вне разрешённого корня", out)

    def test_edit_file_reports_bad_arguments(self):
        tmp = self._tmp_dir("danybot_ef_")
        (tmp / "a.txt").write_text("alpha", encoding="utf-8")
        self._use_coder_root(tmp)
        cases = (
            (
                {"path": "нет.txt", "old_string": "a", "new_string": "b"},
                "Файл не найден",
            ),
            (
                {"path": "a.txt", "old_string": "", "new_string": "b"},
                "Пустой old_string.",
            ),
            (
                {"path": "a.txt", "old_string": "alpha", "new_string": "alpha"},
                "совпадают",
            ),
            (
                {"path": "a.txt", "old_string": "zz", "new_string": "b"},
                "Фрагмент не найден",
            ),
        )
        for arguments, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, _owner_tool("edit_file", arguments, 1))

    def test_edit_file_requires_replace_all_for_repeats(self):
        tmp = self._tmp_dir("danybot_ef2_")
        (tmp / "a.txt").write_text("x\nx\n", encoding="utf-8")
        self._use_coder_root(tmp)
        out = _owner_tool(
            "edit_file", {"path": "a.txt", "old_string": "x", "new_string": "y"}, 1
        )
        self.assertIn("встречается 2 раз", out)
        out = _owner_tool(
            "edit_file",
            {
                "path": "a.txt",
                "old_string": "x",
                "new_string": "y",
                "replace_all": True,
            },
            1,
        )
        self.assertIn("Изменён", out)
        self.assertEqual((tmp / "a.txt").read_text(encoding="utf-8"), "y\ny\n")

    def test_edit_file_reports_read_error(self):
        tmp = self._tmp_dir("danybot_ef3_")
        (tmp / "a.txt").write_text("alpha", encoding="utf-8")
        self._use_coder_root(tmp)
        with mock.patch.object(Path, "read_text", side_effect=OSError("сбой ввода")):
            out = _owner_tool(
                "edit_file",
                {"path": "a.txt", "old_string": "alpha", "new_string": "b"},
                1,
            )
        self.assertIn("Ошибка чтения", out)

    def test_list_dir_reports_bad_paths(self):
        tmp = self._tmp_dir("danybot_ld_")
        (tmp / "a.txt").write_text("x", encoding="utf-8")
        self._use_coder_root(tmp)
        self.assertIn(
            "вне разрешённого корня", _owner_tool("list_dir", {"path": "/etc"}, 1)
        )
        self.assertIn("Каталог не найден", _owner_tool("list_dir", {"path": "нет"}, 1))
        self.assertIn("Это файл", _owner_tool("list_dir", {"path": "a.txt"}, 1))

    def test_list_dir_reports_empty_and_scandir_error(self):
        tmp = self._tmp_dir("danybot_ld2_")
        (tmp / "sub").mkdir()
        self._use_coder_root(tmp)
        self.assertIn("(пусто)", _owner_tool("list_dir", {"path": "sub"}, 1))

        def failing(_path):
            raise OSError("нет доступа")

        with mock.patch.object(tools_module, "_list_entries", failing):
            out = _owner_tool("list_dir", {"path": "."}, 1)
        self.assertIn("Ошибка чтения каталога", out)

    def test_list_dir_falls_back_to_name_for_broken_entry(self):
        tmp = self._tmp_dir("danybot_ld3_")
        link = tmp / "битый.txt"
        try:
            link.symlink_to(tmp / "нет.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self._use_coder_root(tmp)
        out = _owner_tool("list_dir", {"path": "."}, 1)
        self.assertIn("битый.txt", out)

    def test_search_files_reports_bad_paths_and_patterns(self):
        tmp = self._tmp_dir("danybot_sf_")
        (tmp / "a.txt").write_text("строка", encoding="utf-8")
        self._use_coder_root(tmp)
        self.assertEqual(
            _owner_tool("search_files", {"pattern": ""}, 1), "Пустой pattern."
        )
        self.assertIn(
            "вне разрешённого корня",
            _owner_tool("search_files", {"pattern": "x", "path": "/etc"}, 1),
        )
        self.assertIn(
            "Путь не найден",
            _owner_tool("search_files", {"pattern": "x", "path": "нет"}, 1),
        )
        self.assertIn(
            "Некорректное выражение", _owner_tool("search_files", {"pattern": "["}, 1)
        )

    def test_scan_files_reports_each_budget(self):
        tmp = self._tmp_dir("danybot_scanbudget_")
        for index in range(3):
            (tmp / f"f{index}.txt").write_text("hit\nother\n", encoding="utf-8")

        saved = (
            tools_module.MAX_SEARCH_FILES,
            tools_module.MAX_SEARCH_NODES,
            tools_module.MAX_SEARCH_FILE_BYTES,
        )
        self.addCleanup(
            setattr,
            tools_module,
            "MAX_SEARCH_FILES",
            saved[0],
        )
        self.addCleanup(
            setattr,
            tools_module,
            "MAX_SEARCH_NODES",
            saved[1],
        )
        self.addCleanup(
            setattr,
            tools_module,
            "MAX_SEARCH_FILE_BYTES",
            saved[2],
        )

        matches, note = scan_files(tmp, "*.txt", "hit", 10)
        self.assertEqual(len(matches), 3)
        self.assertEqual(note, "")

        matches, note = scan_files(tmp, "*.txt", "hit", 1)
        self.assertEqual(len(matches), 1)
        self.assertIn("лимит результатов", note)

        tools_module.MAX_SEARCH_FILES = 1
        _matches, note = scan_files(tmp, "*.txt", "hit", 10)
        self.assertIn("просмотрено не больше 1 файлов", note)

        tools_module.MAX_SEARCH_FILES = saved[0]
        tools_module.MAX_SEARCH_NODES = 1
        _matches, note = scan_files(tmp, "*.txt", "hit", 10)
        self.assertIn("обойдено не больше 1 элементов", note)

        tools_module.MAX_SEARCH_NODES = saved[1]
        tools_module.MAX_SEARCH_FILE_BYTES = 1
        matches, note = scan_files(tmp, "*.txt", "hit", 10)
        self.assertEqual(matches, [])
        self.assertIn("пропущено файлов больше 1 байт: 3", note)

    def test_scan_files_stops_when_flag_is_set(self):
        tmp = self._tmp_dir("danybot_scanstop_")
        (tmp / "a.txt").write_text("hit\n", encoding="utf-8")
        stop = threading.Event()
        stop.set()
        matches, note = scan_files(tmp, "*.txt", "hit", 10, stop)
        self.assertEqual(matches, [])
        self.assertIn("остановлено по таймауту", note)

    def test_scan_files_skips_dirs_and_escaped_links(self):
        tmp = self._tmp_dir("danybot_scanlinks_")
        outside = self._tmp_dir("danybot_scanoutside_")
        (tmp / "ok.txt").write_text("hit\n", encoding="utf-8")
        (tmp / "sub").mkdir()
        (tmp / "sub" / "nested.txt").write_text("hit\n", encoding="utf-8")
        (outside / "secret.txt").write_text("hit\n", encoding="utf-8")
        link = tmp / "link.txt"
        try:
            link.symlink_to(outside / "secret.txt")
        except (OSError, NotImplementedError):
            self.skipTest("симлинки недоступны")
        matches, _note = scan_files(tmp, "*", "hit", 10)
        self.assertTrue(any("ok.txt" in item for item in matches))
        self.assertTrue(any("nested.txt" in item for item in matches))
        self.assertFalse(any("link.txt" in item for item in matches))

    def test_reconfigure_stream_tolerates_any_target(self):
        class _NoReconfigure:
            pass

        class _Broken:
            def reconfigure(self, **_kwargs):
                raise ValueError("поток закрыт")

        tools_module._reconfigure_stream(_NoReconfigure())
        tools_module._reconfigure_stream(_Broken())
        tools_module._reconfigure_stream(object())

    def test_scan_worker_main_reports_payload(self):
        tmp = self._tmp_dir("danybot_scanworker_")
        (tmp / "a.txt").write_text("hit\n", encoding="utf-8")
        request = json.dumps(
            {
                "root": str(tmp),
                "glob": "*.txt",
                "pattern": "hit",
                "limit": 5,
                "limits": {"MAX_SEARCH_NODES": 100},
            }
        )
        payload = _run_scan_worker(request)
        self.assertEqual(payload["error"], "")
        self.assertEqual(len(payload["matches"]), 1)
        self.assertIn("a.txt", payload["matches"][0])

    def test_scan_worker_main_reports_bad_request(self):
        tmp = self._tmp_dir("danybot_scanworker2_")
        payload = _run_scan_worker(
            json.dumps(
                {
                    "root": str(tmp),
                    "glob": "*",
                    "pattern": "[",
                    "limit": 5,
                    "limits": {},
                }
            )
        )
        self.assertEqual(payload["matches"], [])
        self.assertIn("unterminated", payload["error"])
        with self.assertRaises(json.JSONDecodeError):
            _run_scan_worker("{не json")

    def test_search_files_reports_scanner_failures(self):
        tmp = self._tmp_dir("danybot_sf3_")
        (tmp / "a.txt").write_text("строка\n", encoding="utf-8")
        self._use_coder_root(tmp)

        async def broken(*_args, **_kwargs):
            raise OSError("нет интерпретатора")

        with mock.patch.object(tools_module, "_run_search_subprocess", broken):
            self.assertIn(
                "Ошибка запуска сканера",
                _owner_tool("search_files", {"pattern": "x"}, 1),
            )

        class _FakeProc:
            pid = 1
            returncode = 1

            async def communicate(self, _data=None):
                return b"", "нет модуля tools".encode()

        with mock.patch.object(
            tools_module.asyncio, "create_subprocess_exec", broken_async_exec(_FakeProc)
        ):
            self.assertIn(
                "сканер поиска не отработал",
                _owner_tool("search_files", {"pattern": "x"}, 1),
            )

    def test_search_files_reports_scanner_garbage(self):
        tmp = self._tmp_dir("danybot_sf4_")
        (tmp / "a.txt").write_text("строка\n", encoding="utf-8")
        self._use_coder_root(tmp)

        class _Garbage:
            pid = 1
            returncode = 0

            async def communicate(self, _data=None):
                return "не json".encode(), b""

        class _Failed:
            pid = 1
            returncode = 0

            async def communicate(self, _data=None):
                return b'{"matches": [], "note": "", "error": "boom"}', b""

        for proc, expected in (
            (_Garbage(), "сканер поиска вернул мусор"),
            (_Failed(), "сканер поиска упал"),
        ):
            with (
                self.subTest(expected=expected),
                mock.patch.object(
                    tools_module.asyncio,
                    "create_subprocess_exec",
                    broken_async_exec(proc),
                ),
            ):
                self.assertIn(
                    expected, _owner_tool("search_files", {"pattern": "x"}, 1)
                )

    def test_execute_script_reports_launch_error(self):
        async def broken(*_args, **_kwargs):
            raise OSError("нет интерпретатора")

        with mock.patch.object(tools_module.asyncio, "create_subprocess_exec", broken):
            self.assertIn(
                "Ошибка запуска",
                _owner_tool("execute_script", {"code": "print(1)"}, 1),
            )

    def test_run_shell_reports_launch_error(self):
        async def broken(*_args, **_kwargs):
            raise OSError("нет оболочки")

        with mock.patch.object(tools_module.asyncio, "create_subprocess_shell", broken):
            self.assertIn(
                "Ошибка запуска", _owner_tool("run_shell", {"command": "echo 1"}, 1)
            )

    def test_search_files_reports_total_timeout(self):
        tmp = self._tmp_dir("danybot_sf2_")
        (tmp / "a.txt").write_text("строка", encoding="utf-8")
        self._use_coder_root(tmp)
        saved = tools_module.SEARCH_TIMEOUT
        self.addCleanup(setattr, tools_module, "SEARCH_TIMEOUT", saved)
        tools_module.SEARCH_TIMEOUT = 0
        out = _owner_tool("search_files", {"pattern": "строка"}, 1)
        self.assertIn("Таймаут поиска", out)

    def test_search_files_matches_whole_line_pattern(self):
        tmp = self._tmp_dir("danybot_sf3_")
        (tmp / "a.txt").write_text("игла\nигол", encoding="utf-8")
        self._use_coder_root(tmp)
        out = _owner_tool("search_files", {"pattern": "игла$"}, 1)
        self.assertIn("a.txt:1", out)
        self.assertNotIn("a.txt:2", out)


class StartupTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.saved_token = userbot.BOT_TOKEN
        self.addCleanup(setattr, userbot, "BOT_TOKEN", self.saved_token)
        self.saved_api = (userbot.API_ID, userbot.API_HASH)
        self.addCleanup(setattr, userbot, "API_ID", self.saved_api[0])
        self.addCleanup(setattr, userbot, "API_HASH", self.saved_api[1])
        for attr in ("bot_client", "bot_username", "bot_id"):
            self.addCleanup(setattr, bot, attr, getattr(bot, attr))
        self.connected: list = []
        userbot.API_ID = 123
        userbot.API_HASH = "hash"
        userbot.BOT_TOKEN = NO_AUTH
        self._single_dc()

    def _install_proxies(self, values):
        async def candidates():
            return list(values)

        for module in (userbot, bot):
            self.enterContext(
                mock.patch.object(module, "_proxy_candidates", candidates)
            )

    def _install_client(self, mod, factory, attr):
        saved = getattr(mod, attr)
        self.addCleanup(setattr, mod, attr, saved)
        setattr(mod, attr, factory)

    def _single_dc(self):
        self.enterContext(
            mock.patch.object(
                core,
                "dc_candidates",
                lambda *a, **k: [{"dc": 2, "address": core.DC_ADDRESSES[2]}],
            )
        )

    def test_userbot_requires_credentials(self):
        userbot.API_ID = 0
        self.assertIsNone(asyncio.run(userbot.start_userbot()))

    def test_bot_requires_token(self):
        self._install_proxies([])
        self.assertIsNone(asyncio.run(bot.start_bot()))

    def test_userbot_connects_through_second_proxy(self):
        proxy_a = {"proxy_type": "socks5", "addr": "1.1.1.1", "port": 1080}
        proxy_b = {"proxy_type": "socks5", "addr": "2.2.2.2", "port": 1080}
        self._install_proxies([proxy_a, proxy_b])
        client = _FakeTelegramClient(fail_times=1, username="meuser")
        self._install_client(userbot, lambda _dc=None: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.starts, 2)
        self.assertEqual(client.proxies, [proxy_a, proxy_b])
        self.assertEqual(client.disconnects, 1)
        self.assertEqual(proxies.load_proxy_cache(), [])

    def test_userbot_without_proxies_uses_direct_connection(self):
        self._install_proxies([])
        client = _FakeTelegramClient(username="meuser")
        self._install_client(userbot, lambda _dc=None: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.proxies, [])
        self.assertEqual(client.starts, 1)

    def test_userbot_stops_on_auth_key_error(self):
        self._install_proxies([{"proxy_type": "socks5", "addr": "3.3.3.3", "port": 1}])
        client = _FakeTelegramClient(
            error=AuthKeyError(request=None, message="AUTH_KEY_UNREGISTERED")
        )
        self._install_client(userbot, lambda _dc=None: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.starts, 1)
        self.assertEqual(client.disconnects, 1)

    def test_userbot_gives_up_after_all_proxies(self):
        self._install_proxies(
            [
                {"proxy_type": "socks5", "addr": "4.4.4.4", "port": 1},
                {"proxy_type": "http", "addr": "5.5.5.5", "port": 2},
            ]
        )
        client = _FakeTelegramClient(fail_times=5)
        self._install_client(userbot, lambda _dc=None: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.starts, 2)
        self.assertEqual(client.proxies[-1]["addr"], "5.5.5.5")

    def test_userbot_survives_set_proxy_failure(self):
        self._install_proxies([{"proxy_type": "socks5", "addr": "6.6.6.6", "port": 1}])
        client = _FakeTelegramClient(username="meuser")
        client.set_proxy = mock.Mock(side_effect=RuntimeError("still connected"))
        self._install_client(userbot, lambda _dc=None: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.starts, 0)

    def _install_connect(self, clients):
        queue = list(clients)
        seen = []

        def connect(_proxy, _dc=None, _address=""):
            seen.append(_proxy)
            return queue.pop(0)

        saved = bot._connect
        self.addCleanup(setattr, bot, "_connect", saved)
        bot._connect = connect
        self.connected = seen

    def test_bot_connects_and_sets_commands(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        self._install_proxies([None])
        client = _FakeAiogramClient(username="danybot_bot", uid=77)
        self._install_connect([client])
        self.assertIsNone(asyncio.run(bot.start_bot()))
        self.assertEqual(bot.bot_username, "danybot_bot")
        self.assertEqual(bot.bot_id, 77)
        self.assertEqual(bot.bot_client, client)
        self.assertEqual(client.commands, 1)
        self.assertEqual(client.polled, 1)
        self.assertEqual(self.connected, [None])

    def test_bot_tries_next_proxy_after_network_error(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        proxy = {"proxy_type": "socks5", "addr": "7.7.7.7", "port": 1080}
        self._install_proxies([proxy, None])
        first = _FakeAiogramClient(error=TelegramNetworkError(_NO_METHOD, "timeout"))
        second = _FakeAiogramClient(username="danybot_bot", uid=9)
        self._install_connect([first, second])
        marked = []
        self.enterContext(
            mock.patch.object(
                proxies, "mark_bad_proxy", lambda item: marked.append(item)
            )
        )
        asyncio.run(bot.start_bot())
        self.assertEqual(bot.bot_id, 9)
        self.assertEqual(first.closed, 1)
        self.assertEqual(second.polled, 1)
        self.assertEqual(marked, [("socks5", "7.7.7.7", 1080)])

    def test_bot_gives_up_after_all_proxies(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        self._install_proxies(
            [
                {"proxy_type": "socks5", "addr": "8.8.8.8", "port": 1},
                {"proxy_type": "http", "addr": "9.9.9.9", "port": 2},
            ]
        )
        clients = [
            _FakeAiogramClient(error=OSError("connect failed")),
            _FakeAiogramClient(error=TelegramNetworkError(_NO_METHOD, "timeout")),
        ]
        self._install_connect(clients)
        self.enterContext(mock.patch.object(proxies, "mark_bad_proxy", lambda _i: None))
        asyncio.run(bot.start_bot())
        self.assertEqual([c.polled for c in clients], [0, 0])
        self.assertEqual([c.closed for c in clients], [1, 1])
        self.assertIsNone(bot.bot_client)

    def test_bot_logs_inline_disabled(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        saved_mode = bot.inline_mode
        self.addCleanup(setattr, bot, "inline_mode", saved_mode)
        bot.inline_mode = False
        self._install_proxies([None])
        client = _FakeAiogramClient(username="danybot_bot", uid=77)
        self._install_connect([client])
        with self.assertLogs("danybot.bot", level="INFO") as logs:
            asyncio.run(bot.start_bot())
        self.assertTrue(
            any("Инлайн-режим выключен" in line for line in logs.output),
            logs.output,
        )
        self.assertEqual(client.polled, 1)

    def test_session_for_skips_resolver_when_unsupported(self):
        class _PlainSession:
            def __init__(self, **_kwargs):
                self.kwargs = _kwargs

        with (
            mock.patch.object(bot, "AiohttpSession", _PlainSession),
            self.assertLogs("danybot.bot", level="DEBUG") as logs,
        ):
            session = bot._session_for(None, 2)
        self.assertIsInstance(session, _PlainSession)
        self.assertTrue(
            any("не поддерживает фиксацию адреса ДЦ" in line for line in logs.output),
            logs.output,
        )

    def test_bot_stops_on_invalid_token(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        self._install_proxies([None])
        client = _FakeAiogramClient(
            error=TelegramUnauthorizedError(_NO_METHOD, "Unauthorized")
        )
        self._install_connect([client])
        asyncio.run(bot.start_bot())
        self.assertEqual(client.closed, 1)
        self.assertEqual(client.polled, 0)

    def test_bot_stops_on_malformed_token(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        self._install_proxies([None])
        self.enterContext(
            mock.patch.object(
                bot, "_connect", mock.Mock(side_effect=TokenValidationError("bad"))
            )
        )
        asyncio.run(bot.start_bot())
        self.assertIsNone(bot.bot_client)

    def test_bot_skips_proxy_when_session_cannot_be_built(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        proxy = {"proxy_type": "socks5", "addr": "4.4.4.4", "port": 1}
        self._install_proxies([proxy, None])
        second = _FakeAiogramClient(username="danybot_bot", uid=3)
        self.enterContext(
            mock.patch.object(
                bot,
                "_connect",
                mock.Mock(side_effect=[RuntimeError("no aiohttp-socks"), second]),
            )
        )
        asyncio.run(bot.start_bot())
        self.assertEqual(bot.bot_id, 3)
        self.assertEqual(second.polled, 1)


class MainRunTest(BotTestCase):
    def setUp(self):
        super().setUp()
        import main as main_module

        self.main = main_module
        runtime = dict(subagents._RUNTIME)
        self.addCleanup(subagents._RUNTIME.update, runtime)
        for mod, attr, value in (
            (userbot, "ENABLE_USERBOT", True),
            (userbot, "ENABLE_BOT", True),
        ):
            saved = getattr(mod, attr)
            self.addCleanup(setattr, mod, attr, saved)
            setattr(mod, attr, value)
        for target, attr in (
            (userbot, "load_state"),
            (userbot, "load_history"),
        ):
            saved = getattr(target, attr)
            self.addCleanup(setattr, target, attr, saved)

            def sync_noop(*_args, **_kwargs):
                return None

            setattr(target, attr, sync_noop)
        for target, attr in (
            (userbot, "refresh_models"),
            (userbot, "disconnect_quietly"),
            (userbot, "close_ai"),
            (bot, "disconnect_quietly"),
        ):
            saved = getattr(target, attr)
            self.addCleanup(setattr, target, attr, saved)

            async def noop(*_args, **_kwargs):
                return None

            setattr(target, attr, noop)
        for target, attr in (
            (userbot, "HISTORY_SAVER"),
            (bot, "HISTORY_SAVER"),
        ):
            saved = getattr(target, attr)
            self.addCleanup(setattr, target, attr, saved)
            setattr(target, attr, _FakeSaver())
        saved_close = tools_module.close_httpx_client
        self.addCleanup(setattr, tools_module, "close_httpx_client", saved_close)

        async def close_client():
            pass

        tools_module.close_httpx_client = close_client

    def test_run_reports_each_mode_once(self):
        async def instant():
            pass

        async def slow():
            await asyncio.sleep(0.05)

        for target, attr, value in (
            (userbot, "start_userbot", instant),
            (bot, "start_bot", slow),
        ):
            saved = getattr(target, attr)
            self.addCleanup(setattr, target, attr, saved)
            setattr(target, attr, value)
        with self.assertLogs("danybot.main", level="WARNING") as logs:
            asyncio.run(self.main.run())
        stopped = [
            rec for rec in logs.records if "Режим завершился" in rec.getMessage()
        ]
        self.assertEqual(len(stopped), 2)

    def test_run_reports_mode_error_once(self):
        async def broken():
            raise RuntimeError("mode down")

        async def slow():
            await asyncio.sleep(0.05)

        for target, attr, value in (
            (userbot, "start_userbot", broken),
            (bot, "start_bot", slow),
        ):
            saved = getattr(target, attr)
            self.addCleanup(setattr, target, attr, saved)
            setattr(target, attr, value)
        with self.assertLogs("danybot.main", level="WARNING") as logs:
            asyncio.run(self.main.run())
        errors = [rec for rec in logs.records if "ошибкой" in rec.getMessage()]
        self.assertEqual(len(errors), 1)
        self.assertIn("mode down", errors[0].getMessage())

    def test_run_stops_when_no_mode_enabled(self):
        userbot.ENABLE_USERBOT = False
        userbot.ENABLE_BOT = False
        with self.assertLogs("danybot.main", level="ERROR") as logs:
            asyncio.run(self.main.run())
        self.assertTrue(
            any("Не включён ни один режим" in line for line in logs.output), logs.output
        )

    def test_run_stops_on_registered_signal(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            handlers = {}

            def fake_add(sig, callback, *_args):
                handlers[sig] = callback

            async def slow():
                await asyncio.sleep(5)

            for target, attr, value in (
                (userbot, "start_userbot", slow),
                (bot, "start_bot", slow),
            ):
                saved = getattr(target, attr)
                self.addCleanup(setattr, target, attr, saved)
                setattr(target, attr, value)

            async def fire():
                await asyncio.sleep(0.02)
                self.assertTrue(handlers)
                for callback in handlers.values():
                    callback()

            with mock.patch.object(loop, "add_signal_handler", fake_add):
                asyncio.ensure_future(fire())
                await self.main.run()

        asyncio.run(scenario())

    def test_run_logs_cancelled_mode_once(self):
        async def self_cancel():
            current = asyncio.current_task()
            if current is not None:
                current.cancel()
            await asyncio.sleep(0)

        async def slow():
            await asyncio.sleep(5)

        for target, attr, value in (
            (userbot, "start_userbot", self_cancel),
            (bot, "start_bot", slow),
        ):
            saved = getattr(target, attr)
            self.addCleanup(setattr, target, attr, saved)
            setattr(target, attr, value)
        with self.assertLogs("danybot.main", level="WARNING") as logs:
            asyncio.run(self.main.run())
        stopped = [rec for rec in logs.records if "Режим прерван" in rec.getMessage()]
        self.assertEqual(len(stopped), 1)

    def test_run_swallow_outer_cancellation(self):
        async def scenario():
            task = asyncio.ensure_future(self.main.run())
            await asyncio.sleep(0.02)
            task.cancel()
            return await task

        self.assertIsNone(asyncio.run(scenario()))

    def test_main_entry_swallows_keyboard_interrupt(self):
        async def interrupted():
            raise KeyboardInterrupt

        saved = self.main.run
        self.addCleanup(setattr, self.main, "run", saved)
        self.main.run = interrupted
        self.main.main()


def run_unit_tests():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    return runner.run(suite)


def find_spec_or_none(module):
    try:
        import importlib.util

        return importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None


def resolve_tool(kind, value):
    if kind == "exe":
        return [value] if shutil.which(value) else None
    if kind == "module":
        return [sys.executable, "-m", value] if find_spec_or_none(value) else None
    return list(value)


def build_linters():
    specs = [
        ("compileall", "raw", [sys.executable, "-m", "compileall", "-q", *PY_FILES]),
        ("pyflakes", "exe", "pyflakes"),
        ("flake8", "exe", "flake8"),
        ("ruff-check", "exe", "ruff"),
        ("black", "exe", "black"),
        ("isort", "exe", "isort"),
        ("pylint", "module", "pylint"),
        ("vulture", "exe", "vulture"),
        ("bandit", "module", "bandit"),
        ("mypy", "module", "mypy"),
        ("pyright", "module", "pyright"),
        ("radon", "exe", "radon"),
        ("codespell", "exe", "codespell"),
        ("pip-audit", "exe", "pip-audit"),
    ]
    commands = []
    for name, kind, value in specs:
        cmd = resolve_tool(kind, value)
        if cmd is None:
            commands.append((name, None))
            continue
        if name == "ruff-check":
            cmd += ["check", "--no-cache", *PY_FILES]
        elif name == "vulture":
            cmd += [
                *PY_FILES,
                WHITELIST_FILE,
                "--min-confidence",
                "60",
                "--ignore-names",
                VULTURE_IGNORE_NAMES,
            ]
        elif name == "bandit":
            cmd += ["-q", "--skip", BANDIT_SKIP, *PY_FILES]
        elif name == "mypy":
            cmd += ["--ignore-missing-imports", "--no-strict-optional", *PY_FILES]
        elif name == "pyright":
            cmd += ["--pythonpath", sys.executable, *PY_FILES]
        elif name == "pylint":
            cmd += [
                "--disable=all",
                "--enable=F,E,W",
                "--disable=W0603,W0212,W0613,W0621,W0622,W0404,W0108",
                "--max-line-length=120",
                *PY_FILES,
            ]
        elif name == "flake8":
            cmd += [
                "--max-line-length",
                "120",
                "--extend-ignore",
                "E203,W503",
                "--per-file-ignores",
                "proxies.py:E501",
                *PY_FILES,
            ]
        elif name == "black":
            cmd += ["--check", *PY_FILES]
        elif name == "isort":
            cmd += [
                "--check-only",
                "--profile",
                "black",
                "-p",
                "main,bot,userbot,proxies,core,subagents,memory,skills",
                *PY_FILES,
            ]
        elif name == "radon":
            cmd += ["cc", "-s", "-a", *PY_FILES]
        elif name == "codespell":
            cmd += [*PY_FILES]
        elif name == "pip-audit":
            cmd += ["-r", REQUIREMENTS_FILE]
        commands.append((name, cmd))
    commands.append(("coverage", []))

    fmt_cmd = resolve_tool("exe", "ruff")
    if fmt_cmd is not None:
        commands.insert(4, ("ruff-format", [*fmt_cmd, "format", "--check", *PY_FILES]))
    else:
        commands.insert(4, ("ruff-format", None))
    return commands


def extract_note(name, proc):
    if name == "radon":
        for line in proc.stdout.splitlines():
            if line.startswith("Average complexity"):
                return line.strip()[:110]
    if name == "coverage":
        for line in proc.stdout.splitlines():
            if line.startswith("TOTAL"):
                return f"покрытие {line.split()[-1]}"
    if name == "pip-audit":
        for line in proc.stdout.splitlines():
            if "No known vulnerabilities" in line:
                return "уязвимостей не найдено"
    return ""


def run_coverage():
    base = [sys.executable, "-m", "coverage"]
    run_logged([*base, "erase"])
    run_proc, elapsed1 = run_logged(
        [
            *base,
            "run",
            "--source=" + COVERAGE_SOURCE,
            "-m",
            "unittest",
            "discover",
            "-s",
            ".",
            "-p",
            "tests.py",
        ]
    )
    if run_proc.returncode != 0:
        tail = (run_proc.stdout + run_proc.stderr).strip().splitlines()
        return ("coverage", "FAIL", " | ".join(tail[-2:])[:120], elapsed1)
    rep, elapsed2 = run_logged([*base, "report"])
    if rep.returncode != 0:
        tail = (rep.stdout + rep.stderr).strip().splitlines()
        return ("coverage", "FAIL", " | ".join(tail[-2:])[:120], elapsed1 + elapsed2)
    note = extract_note("coverage", rep)
    if not note:
        return ("coverage", "FAIL", "отчёт пуст", elapsed1 + elapsed2)
    return ("coverage", "PASS", note, elapsed1 + elapsed2)


def run_linters():
    results = []
    tools = build_linters()
    total = len(tools)
    for idx, (name, cmd) in enumerate(tools, start=1):
        print(f"[{idx:>2}/{total}] {name}")
        if name == "coverage":
            if find_spec_or_none("coverage"):
                results.append(run_coverage())
            else:
                results.append((name, "SKIP", "инструмент не найден", 0.0))
                print("      SKIP: инструмент не найден")
            continue
        if cmd is None:
            results.append((name, "SKIP", "инструмент не найден", 0.0))
            print(f"      $ {_cmd_str(cmd) if cmd else name}")
            print("      SKIP: инструмент не найден")
            continue
        print(f"      $ {_cmd_str(cmd)}")
        proc, elapsed = run_logged(cmd)
        status = "PASS" if proc.returncode == 0 else "FAIL"
        note = ""
        if proc.returncode == 0:
            note = extract_note(name, proc)
        else:
            combined = proc.stdout + proc.stderr
            if name == "pip-audit" and (
                "Traceback" in combined or "Connection" in combined
            ):
                status = "SKIP"
                note = "нет доступа к сети или реестру"
            else:
                tail = combined.strip().splitlines()
                note = " | ".join(tail[-3:])[:120]
        results.append((name, status, note, elapsed))
        print(f"      rc={proc.returncode} · {elapsed:.2f}s · {status}")
        if note:
            print(f"      {note}")
    return results


def print_summary(unit_result, lint_results):
    failures = 0
    errors = 0
    skipped = 0
    total = 0
    if unit_result is not None:
        total = unit_result.testsRun
        failures = len(unit_result.failures)
        errors = len(unit_result.errors)
        skipped = len(unit_result.skipped)
    print()
    print("=" * 64)
    print("СВОДКА")
    print("=" * 64)
    if unit_result is not None:
        print(
            f"Юнит-тесты : {total - failures - errors - skipped}/{total} OK, "
            f"fail={failures}, error={errors}, skip={skipped}"
        )
    else:
        print("Юнит-тесты : ПРОПУЩЕНО")
    for name, status, note, _elapsed in lint_results:
        line = f"{name:<13} {status:<5}"
        if note:
            line += f"  {note}"
        print(line)
    print("=" * 64)
    lint_failed = any(status == "FAIL" for _, status, _, _ in lint_results)
    nothing_ran = unit_result is None and not lint_results
    verdict_ok = (
        not nothing_ran
        and (unit_result is None or unit_result.wasSuccessful())
        and not lint_failed
    )
    print("ВЕРДИКТ   :", "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ" if verdict_ok else "ЕСТЬ ПРОВАЛЫ")
    return verdict_ok


def main(argv=None):
    _reconfigure_stdio()
    log_open()
    try:
        parser = argparse.ArgumentParser(description="DanyBOT self-check runner")
        parser.add_argument("--skip-unit", action="store_true")
        parser.add_argument("--skip-lint", action="store_true")
        args = parser.parse_args(argv)

        unit_result = None
        lint_results = []
        if not args.skip_unit:
            print("--- Юнит-тесты ---")
            unit_result = run_unit_tests()
        if not args.skip_lint:
            print("--- Линтеры и статический анализ ---")
            lint_results = run_linters()

        ok = print_summary(unit_result, lint_results)
        return 0 if ok else 1
    finally:
        log_close()


if __name__ == "__main__":
    sys.exit(main())
