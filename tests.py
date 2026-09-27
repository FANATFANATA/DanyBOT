import argparse
import asyncio
import json
import math
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from unittest import mock

import httpx
from telethon.crypto import AuthKey
from telethon.errors import AuthKeyError, FloodWaitError, RPCError
from telethon.sessions import StringSession

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
BANDIT_SKIP = "B404,B603,B608"
VULTURE_IGNORE_NAMES = "test_*,setUp"
LOG_FILE = PROJECT_DIR / "toolrun.log"
BOT_AUTH_VALUE = "bot-token"
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


_MODE_ATTRS = (
    "model_overrides",
    "coder_chats",
    "reasoning_hidden",
    "tools_hidden",
    "chat_history",
)


def _snapshot_module(mod):
    snap = {}
    for attr in _MODE_ATTRS:
        value = getattr(mod, attr)
        if attr == "chat_history":
            snap[attr] = {k: deque(v, maxlen=v.maxlen) for k, v in value.items()}
        elif attr == "model_overrides":
            snap[attr] = dict(value)
        else:
            snap[attr] = set(value)
    return snap


def _restore_module(mod, snap):
    for attr, value in snap.items():
        setattr(mod, attr, value)


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
    data = getattr(btn, "data", None)
    if data is None:
        data = getattr(getattr(btn, "type", None), "data", None)
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)


def rows_data(rows):
    return {btn_data(btn) for row in rows for btn in row}


class BotTestCase(unittest.TestCase):
    def setUp(self):
        patch_paths(self)
        self._snap = snapshot_mode_state()

        def restore_snap():
            restore_mode_state(self._snap)

        self.addCleanup(restore_snap)


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

    def test_tool_round_limit_reached(self):
        endless = [
            make_chunk(make_delta(tool_calls=[make_tc(tc_id="c1", name="evaluate")]))
        ]
        self.install_ai([endless, endless])
        with mock.patch.object(userbot, "MAX_TOOL_ROUNDS", 2):
            answer = asyncio.run(
                userbot.stream_with_tools(
                    [],
                    "m",
                    self.CHAT_ID,
                    lambda p: self.collect([], p),
                    lambda p: self.collect([], p),
                )
            )
        self.assertEqual(answer, "Достигнут лимит циклов инструментов.")

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
            "(пустой ответ / empty answer)",
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

    def test_async_saver_mark_dirty_without_loop(self):
        saver = core.AsyncSaver(lambda: None)
        saver.mark_dirty()
        self.assertTrue(saver._dirty)
        self.assertIsNone(saver._task)

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
        rows = bot._settings_rows(self.CHAT_ID)
        data = {btn_data(btn) for row in rows for btn in row}
        self.assertIn("settings:prompt", data)
        source = (PROJECT_DIR / "bot.py").read_text(encoding="utf-8")
        menu_block = source[
            source.index("SetBotCommandsRequest") : source.index(
                "]", source.index("SetBotCommandsRequest")
            )
        ]
        self.assertIn('command="settings"', menu_block)
        self.assertNotIn('command="prompt"', menu_block)


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


class _FakeMessage:
    def __init__(self, text, msg_id=1, out=False, is_reply=False, reply_msg=None):
        self.message = text
        self.id = msg_id
        self.out = out
        self.is_reply = is_reply
        self._reply_msg = reply_msg
        self.sender_id = 1
        self.chat_id = 1

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
        return _FakeHandlerEvent(
            _FakeMessage(text, **kwargs),
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

    def test_history_saver_marked_dirty(self):
        self._run(self._group_event("@danybot привет"))
        self.assertGreaterEqual(self.saver.dirty, 1)

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
        start = source.index("SetBotCommandsRequest")
        menu_block = source[start : source.index("]", start)]
        menu = {
            line.split('command="', 1)[1].split('"', 1)[0]
            for line in menu_block.splitlines()
            if 'command="' in line
        }
        help_block = source[source.index("BOT_HELP_TEXT = (") :]
        help_block = help_block[: help_block.index("\n)\n")]
        for name in menu:
            self.assertIn(f"/{name}", help_block)
        for name in ("settings",):
            self.assertIn(name, menu)

    def test_bot_handler_uses_coder_tools(self):
        source = (PROJECT_DIR / "bot.py").read_text(encoding="utf-8")
        self.assertIn("tool_menu = tools_module.CODER_TOOLS", source)
        self.assertIn("tool_menu = tools_module.BOT_TOOLS", source)
        self.assertIn("tool_menu = tools_module.PUBLIC_TOOLS", source)
        self.assertIn('mode = "coder" if coder_active else "bot"', source)

    def test_db_trigger_never_active(self):
        saved = userbot.ENABLE_USERBOT
        self.addCleanup(setattr, userbot, "ENABLE_USERBOT", saved)
        userbot.ENABLE_USERBOT = True
        self.assertFalse(bot._db_triggered(".db привет"))
        self.assertFalse(bot._db_triggered(".ai привет"))
        userbot.ENABLE_USERBOT = False
        self.assertFalse(bot._db_triggered(".db привет"))
        self.assertFalse(bot._db_triggered("привет .ai"))
        self.assertFalse(bot._db_triggered("привет"))
        self.assertFalse(bot._db_triggered(None))

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
    def __init__(self, data, chat_id, sender_id):
        self.data = data
        self.chat_id = chat_id
        self.sender_id = sender_id
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
                "settings:clear",
                "settings:prompt",
            },
        )

    def test_settings_text_reports_state(self):
        text = bot._settings_text(self.CHAT_ID)
        self.assertIn("Настройки / Settings", text)
        self.assertIn(userbot.DANYAPI_MODEL, text)
        self.assertIn("Контекст / Context", text)

    def test_callback_toggles_visibility(self):
        bot.reasoning_hidden.discard(self.CHAT_ID)
        bot.tools_hidden.discard(self.CHAT_ID)
        event = _CallbackEvent(b"settings:reasoning", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn(self.CHAT_ID, bot.reasoning_hidden)
        event = _CallbackEvent(b"settings:tools", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertIn(self.CHAT_ID, bot.tools_hidden)

    def test_callback_toggles_coder(self):
        bot.coder_chats.discard(self.CHAT_ID)
        event = _CallbackEvent(b"settings:coder", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertTrue(bot.is_coder(self.CHAT_ID))

    def test_callback_rejects_non_owner(self):
        event = _CallbackEvent(b"settings:reasoning", self.CHAT_ID, self.CHAT_ID + 1)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(event.edits, [])
        self.assertTrue(event.answers[-1][1])

    def test_callback_clear_resets_context(self):
        self.addCleanup(bot.chat_history.pop, self.CHAT_ID, None)
        bot.chat_history[self.CHAT_ID] = deque(
            [{"role": "user", "content": "x"}], maxlen=userbot.DM_HISTORY_LIMIT
        )
        event = _CallbackEvent(b"settings:clear", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(len(bot.chat_history[self.CHAT_ID]), 0)

    def test_callback_model_picker(self):
        userbot.MODELS = ["m-one", "m-two"]
        event = _CallbackEvent(b"settings:model", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        picked = rows_data(event.edits[-1][1])
        self.assertIn("settings:pick:m-one", picked)
        self.assertIn("settings:pick:m-two", picked)
        self.assertIn("settings:main", picked)
        pick = _CallbackEvent(b"settings:pick:m-two", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(pick))
        self.assertEqual(bot.model_overrides[self.CHAT_ID], "m-two")

    def test_callback_unknown_action(self):
        event = _CallbackEvent(b"settings:nope", self.CHAT_ID, self.CHAT_ID)
        asyncio.run(bot.callback_handler(event))
        self.assertEqual(event.edits, [])
        self.assertEqual(event.answers[-1][1], True)

    def test_callback_pick_survives_long_model_name(self):
        long_name = "vendor/" + "x" * 70 + "/model"
        userbot.MODELS = [long_name]
        self.addCleanup(setattr, userbot, "MODELS", userbot.MODELS)
        data = bot._pick_data(long_name)
        self.assertLessEqual(len(data), bot.CALLBACK_MAX_BYTES)
        self.assertEqual(
            bot._resolve_picked_model(data.decode().split(":")[2]), long_name
        )
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
        rows = bot._settings_rows(self.CHAT_ID)
        return next(
            btn.text for row in rows for btn in row if btn_data(btn) == "settings:coder"
        )

    def test_coder_button_shows_on_off(self):
        bot.coder_chats.discard(self.CHAT_ID)
        self.assertIn("выкл", self._coder_button_text())
        bot.coder_chats.add(self.CHAT_ID)
        self.assertIn("вкл", self._coder_button_text())

    def test_visibility_buttons_stay_visibility_wording(self):
        bot.reasoning_hidden.discard(self.CHAT_ID)
        rows = bot._settings_rows(self.CHAT_ID)
        text = next(
            btn.text
            for row in rows
            for btn in row
            if btn_data(btn) == "settings:reasoning"
        )
        self.assertIn("видно", text)


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

    def test_cancelled_worker_is_not_swallowed(self):
        async def cancel(spec):
            raise asyncio.CancelledError

        async def run():
            await subagents._gather_workers(cancel, [{"name": "a", "task": "t"}])

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run())


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
        event = self._event(".db models", msg_id=30)
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

    def test_scan_files_stops_on_directory_flood(self):
        tmp = self._tmp_dir("danybot_scan_")
        deep = tmp
        for i in range(300):
            deep = deep / f"d{i}"
            deep.mkdir()
        (deep / "hit.txt").write_text("needle", encoding="utf-8")
        saved = tools_module.MAX_SEARCH_NODES
        self.addCleanup(setattr, tools_module, "MAX_SEARCH_NODES", saved)
        tools_module.MAX_SEARCH_NODES = 10
        out = _owner_tool("search_files", {"pattern": "needle"}, 1)
        self.assertIn("Совпадений не найдено", out)

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
        self.assertTrue(out.endswith("…"))

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
        env = tools_module._subprocess_env()
        self.assertNotIn("BOT_TOKEN", env)
        self.assertNotIn("DANYAPI_KEY", env)
        self.assertNotIn("API_HASH", env)
        self.assertNotIn("SESSION_NAME", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("MY_PASSWORD", env)
        self.assertEqual(env["KEEP_ME"], "visible")
        self.assertIn("PATH", env)

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
        rows = bot._model_rows(1)
        labels = [row[0].text for row in rows]
        self.assertTrue(any("Ещё 3" in label for label in labels))
        self.assertEqual(len(rows), bot.MODEL_MENU_LIMIT + 2)

    def test_model_menu_has_no_overflow_row_when_short(self):
        saved_models = list(userbot.MODELS)
        self.addCleanup(setattr, userbot, "MODELS", type(userbot.MODELS)(saved_models))
        userbot.MODELS = ["only-one"]
        rows = bot._model_rows(1)
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


class StartupTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.saved_token = userbot.BOT_TOKEN
        self.addCleanup(setattr, userbot, "BOT_TOKEN", self.saved_token)
        self.saved_api = (userbot.API_ID, userbot.API_HASH)
        self.addCleanup(setattr, userbot, "API_ID", self.saved_api[0])
        self.addCleanup(setattr, userbot, "API_HASH", self.saved_api[1])
        userbot.API_ID = 123
        userbot.API_HASH = "hash"
        userbot.BOT_TOKEN = NO_AUTH

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

    def test_userbot_requires_credentials(self):
        userbot.API_ID = 0
        self.assertIsNone(asyncio.run(userbot.start_userbot()))

    def test_bot_requires_token(self):
        self._install_proxies([])
        self.assertIsNone(asyncio.run(bot.start_bot()))

    def test_bot_requires_api_credentials(self):
        userbot.API_ID = 0
        self._install_proxies([])
        self.assertIsNone(asyncio.run(bot.start_bot()))

    def test_userbot_connects_through_second_proxy(self):
        proxy_a = {"proxy_type": "socks5", "addr": "1.1.1.1", "port": 1080}
        proxy_b = {"proxy_type": "socks5", "addr": "2.2.2.2", "port": 1080}
        self._install_proxies([proxy_a, proxy_b])
        client = _FakeTelegramClient(fail_times=1, username="meuser")
        self._install_client(userbot, lambda: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.starts, 2)
        self.assertEqual(client.proxies, [proxy_a, proxy_b])
        self.assertEqual(client.disconnects, 1)
        self.assertEqual(proxies.load_proxy_cache(), [])

    def test_userbot_without_proxies_uses_direct_connection(self):
        self._install_proxies([])
        client = _FakeTelegramClient(username="meuser")
        self._install_client(userbot, lambda: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.proxies, [])
        self.assertEqual(client.starts, 1)

    def test_userbot_stops_on_auth_key_error(self):
        self._install_proxies([{"proxy_type": "socks5", "addr": "3.3.3.3", "port": 1}])
        client = _FakeTelegramClient(
            error=AuthKeyError(request=None, message="AUTH_KEY_UNREGISTERED")
        )
        self._install_client(userbot, lambda: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.starts, 1)
        self.assertEqual(client.disconnects, 0)

    def test_userbot_gives_up_after_all_proxies(self):
        self._install_proxies(
            [
                {"proxy_type": "socks5", "addr": "4.4.4.4", "port": 1},
                {"proxy_type": "http", "addr": "5.5.5.5", "port": 2},
            ]
        )
        client = _FakeTelegramClient(fail_times=5)
        self._install_client(userbot, lambda: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.starts, 2)
        self.assertEqual(client.proxies[-1]["addr"], "5.5.5.5")

    def test_userbot_survives_set_proxy_failure(self):
        self._install_proxies([{"proxy_type": "socks5", "addr": "6.6.6.6", "port": 1}])
        client = _FakeTelegramClient(username="meuser")
        client.set_proxy = mock.Mock(side_effect=RuntimeError("still connected"))
        self._install_client(userbot, lambda: client, "get_client")
        self._install_client(userbot, client, "client")
        asyncio.run(userbot.start_userbot())
        self.assertEqual(client.starts, 0)

    def test_bot_connects_and_sets_commands(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        self._install_proxies([None])
        client = _FakeTelegramClient(username="danybot_bot", uid=77)
        self._install_client(bot, lambda: client, "get_bot_client")
        saved_username = bot.bot_username
        saved_id = bot.bot_id
        self.addCleanup(setattr, bot, "bot_username", saved_username)
        self.addCleanup(setattr, bot, "bot_id", saved_id)
        asyncio.run(bot.start_bot())
        self.assertEqual(bot.bot_username, "danybot_bot")
        self.assertEqual(bot.bot_id, 77)
        self.assertEqual(len(client.commands), 1)
        self.assertFalse(client.running)

    def test_bot_survives_command_registration_error(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        self._install_proxies([None])
        client = _FakeTelegramClient(
            username="danybot_bot",
            uid=5,
            command_error=RPCError(None, "no commands", 400),
        )
        self._install_client(bot, lambda: client, "get_bot_client")
        asyncio.run(bot.start_bot())
        self.assertEqual(bot.bot_username, "danybot_bot")

    def test_bot_stops_on_invalid_token(self):
        userbot.BOT_TOKEN = BOT_AUTH_VALUE
        self._install_proxies([None])
        client = _FakeTelegramClient(
            error=AuthKeyError(request=None, message="AUTH_KEY_UNREGISTERED")
        )
        self._install_client(bot, lambda: client, "get_bot_client")
        asyncio.run(bot.start_bot())
        self.assertEqual(client.starts, 1)


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
        asyncio.run(self.main.run())


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
            "--source=bot,userbot,proxies,tools,core,subagents,memory,skills",
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
    note = extract_note("coverage", rep)
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
    verdict_ok = (
        unit_result is None or unit_result.wasSuccessful()
    ) and not lint_failed
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
