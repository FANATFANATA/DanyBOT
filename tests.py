import argparse
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest import mock

from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

import bot
import core
import proxies
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
    "tests.py",
)
WHITELIST_FILE = "vulture_whitelist.py"
BANDIT_SKIP = "B404,B603"
VULTURE_IGNORE_NAMES = "test_*,setUp"
LOG_FILE = PROJECT_DIR / "toolrun.log"
_log_lines: list[str] = []


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
    ):
        saved.append((mod, attr, getattr(mod, attr)))
        setattr(mod, attr, tmp / fname)

    def restore():
        for mod2, attr2, old in saved:
            setattr(mod2, attr2, old)

    testcase.addCleanup(restore)
    return tmp


def snapshot_bot_state():
    return {
        "model_overrides": dict(userbot.model_overrides),
        "auto_respond": set(userbot.auto_respond),
        "ignored_chats": set(userbot.ignored_chats),
        "ignored_users": set(userbot.ignored_users),
        "chat_history": {
            k: deque(v, maxlen=v.maxlen) for k, v in userbot.chat_history.items()
        },
        "MODELS": list(userbot.MODELS),
    }


def restore_bot_state(snap):
    userbot.model_overrides = snap["model_overrides"]
    userbot.auto_respond = snap["auto_respond"]
    userbot.ignored_chats = snap["ignored_chats"]
    userbot.ignored_users = snap["ignored_users"]
    userbot.chat_history = snap["chat_history"]
    userbot.MODELS = snap["MODELS"]


class BotTestCase(unittest.TestCase):
    def setUp(self):
        patch_paths(self)
        self._snap = snapshot_bot_state()

        def restore_snap():
            restore_bot_state(self._snap)

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

    async def send_message(self, chat, text):
        self.sent.append((chat, text))
        return SimpleNamespace(id=999)

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

    async def get_dialogs(self, limit=30):
        return [
            SimpleNamespace(
                entity=SimpleNamespace(id=i, username=f"user{i}"),
                name=f"name{i}",
            )
            for i in range(1, limit + 1)
        ]

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

    async def __aiter__(self):
        for c in self._chunks:
            yield c


class FakeCompletions:
    def __init__(self, rounds):
        self._rounds = [list(r) for r in rounds]
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return StreamIter(self._rounds.pop(0))


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
        ".danybot",
        ".gpt?",
        ".bot как дела",
        "привет\n.bot после строки",
    )
    NO_MATCH_CASES = (
        "",
        ".",
        "x.db привет",
        ".dbx",
        ".dbмодель",
        "abc.danybot def",
        ".дб привет",
        ".ДБ привет",
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
        (".db очистить", ("clear", None)),
        (".db очистка", ("clear", None)),
        (".db сброс", ("clear", None)),
        (".gpt модель gpt-x", ("model", "gpt-x")),
        (".db model", ("model", None)),
        (".da on", ("autorespond", True)),
        (".авто выкл", ("autorespond", False)),
        (".danyauto да", ("autorespond", True)),
        (".даниавто нет", ("autorespond", False)),
        (".bot help", ("help", None)),
        (".ai ?", ("help", None)),
        (".db ping", ("ping", None)),
        (".дани история", ("history", None)),
        (".данибот модели", ("models", None)),
    )
    NONE_CASES = (
        "hello",
        "",
        ".",
        ".db",
        ".da",
        ".da maybe",
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
        for value in ("0", "false", "no", "", "FALSE", "No", " "):
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
        async def fake_get_working(limit=10, prefer_protocol="socks5"):
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


class StateRoundtripTest(BotTestCase):
    def test_save_load_roundtrip(self):
        userbot.model_overrides = {123: "model-a", -456: "model-b"}
        userbot.auto_respond = {1, -2}
        userbot.ignored_chats = {-100}
        userbot.ignored_users = {777}
        userbot.save_state()
        userbot.model_overrides = {}
        userbot.auto_respond = set()
        userbot.ignored_chats = set()
        userbot.ignored_users = set()
        userbot.load_state()
        self.assertEqual(userbot.model_overrides, {123: "model-a", -456: "model-b"})
        self.assertEqual(userbot.auto_respond, {1, -2})
        self.assertEqual(userbot.ignored_chats, {-100})
        self.assertEqual(userbot.ignored_users, {777})

    def test_corrupt_json_tolerated(self):
        userbot.STATE_FILE.write_text("{not json", encoding="utf-8")
        userbot.model_overrides = {}
        userbot.load_state()
        self.assertEqual(userbot.model_overrides, {})
        self.assertEqual(userbot.auto_respond, set())

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
        for banned in ("<i>", "<b>", "<code>", "\U0001f4ad", "\U0001f527", "\U0001f4ac"):
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

    def test_send_message_to_guards(self):
        empty = asyncio.run(userbot.execute_tool("send_message_to", {}, -100))
        self.assertEqual(empty, "Нужны chat и text.")
        ok = asyncio.run(
            userbot.execute_tool(
                "send_message_to", {"chat": "@me", "text": "hello"}, -100
            )
        )
        self.assertEqual(ok, "Сообщение отправлено.")
        self.assertEqual(self.fake_client.sent, [("@me", "hello")])

    def test_edit_message_guard_and_call(self):
        empty = asyncio.run(
            userbot.execute_tool("edit_message", {"message_id": 1, "text": ""}, -100)
        )
        self.assertEqual(empty, "Пустой текст.")
        ok = asyncio.run(
            userbot.execute_tool("edit_message", {"message_id": 5, "text": "new"}, -100)
        )
        self.assertEqual(ok, "Сообщение отредактировано.")
        self.assertEqual(self.fake_client.edited, [(-100, 5, "new")])

    def test_get_chat_history_formatting_and_clamp(self):
        result = asyncio.run(
            userbot.execute_tool("get_chat_history", {"limit": 150}, -100)
        )
        self.assertEqual(self.fake_client.history_limits[-1], 100)
        lines = result.splitlines()
        self.assertEqual(len(lines), 100)
        self.assertEqual(lines[0], "[1] 1: t1")
        self.assertEqual(lines[-1], "[100] 1: t100")

    def test_get_message_by_id_found_and_missing(self):
        found = asyncio.run(
            userbot.execute_tool("get_message_by_id", {"message_id": 42}, -100)
        )
        self.assertEqual(found, "found")
        missing = asyncio.run(
            userbot.execute_tool("get_message_by_id", {"message_id": 43}, -100)
        )
        self.assertEqual(missing, "Сообщение не найдено.")

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

    def test_list_chats_labels(self):
        result = asyncio.run(userbot.execute_tool("list_chats", {"limit": 3}, -100))
        lines = result.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("1: name1 (@user1)"))

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
        result = asyncio.run(
            userbot.execute_tool("run_shell", {"command": "echo hello"}, self.CHAT_ID)
        )
        self.assertIn("hello", result)

    def test_run_shell_empty(self):
        result = asyncio.run(
            userbot.execute_tool("run_shell", {"command": ""}, self.CHAT_ID)
        )
        self.assertEqual(result, "Пустая команда.")


class BotCommandsTest(BotTestCase):
    CASES: ClassVar[list] = [
        ("/help", ("help", None)),
        ("/start", ("help", None)),
        ("/помощь", ("help", None)),
        ("/справка", ("help", None)),
        ("/model gpt-x", ("model", "gpt-x")),
        ("/модель gpt-x", ("model", "gpt-x")),
        ("/model", ("model", None)),
        ("/models", ("models", None)),
        ("/модели", ("models", None)),
        ("/clear", ("clear", None)),
        ("/очистить", ("clear", None)),
        ("/history", ("history", None)),
        ("/история", ("history", None)),
        ("/ping", ("ping", None)),
        ("/пинг", ("ping", None)),
        ("/auto on", ("autorespond", True)),
        ("/авто выкл", ("autorespond", False)),
        ("/auto off", ("autorespond", False)),
        ("/auto", ("auto_status", None)),
        ("/help@DanyBOTAPI_bot", ("help", None)),
        ("  /PING  ", ("ping", None)),
    ]

    NONE_CASES: ClassVar[list] = [
        "help",
        "/unknown",
        "/",
        "/ignore",
        "/unignore",
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
            "get_chat_history",
            "list_chats",
            "send_message_to",
            "get_chat_history_in",
            "evaluate",
            "get_chat_info",
            "get_user_info",
            "edit_message",
            "get_message_by_id",
            "get_profile",
            "run_shell",
            "web_search",
            "fetch_url",
            "run_subagent",
            "pin_message",
            "unpin_message",
            "get_pinned_messages",
            "react_to_message",
            "get_last_messages",
            "search_messages",
            "send_message",
            "delete_message",
            "forward_message",
            "create_poll",
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
        for val in ("0", "false", "no", "False", "NO", " 0 "):
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

        async def fake_execute(name, args, chat_id):
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

    def test_small_max_tokens_for_other_tools(self):
        fake_ai = self.install_ai(NonStreamResponse(NonStreamMessage(content="ALLOW")))
        ok = asyncio.run(userbot.verify_tool_call("fetch_url", {}, "m"))
        self.assertTrue(ok)
        self.assertEqual(fake_ai.chat.completions.calls[0]["max_tokens"], 8)


class FloodRetryTest(BotTestCase):
    def make_event(self, script):
        attempts = {"n": 0}

        class FakeEvent:
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
        self.assertIn(777, userbot.recent_reply_ids)

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
        self.pinned = []
        self.unpinned = []
        self.deleted = []
        self.forwarded = []
        self.files = []
        self.requests = []

    async def pin_message(self, chat, msg_id, notify=False):
        self.pinned.append((chat, msg_id, notify))

    async def unpin_message(self, chat, msg_id):
        self.unpinned.append((chat, msg_id))

    async def delete_messages(self, chat, ids):
        self.deleted.append((chat, list(ids)))

    async def forward_messages(self, target, msg_id, from_chat):
        self.forwarded.append((target, msg_id, from_chat))

    async def send_file(self, chat, file):
        self.files.append((chat, file))

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace()

    async def get_messages(self, chat, limit=20, ids=None, filter=None, search=None):
        if filter is not None:
            return [SimpleNamespace(id=5, sender_id=1, message="pinned")]
        return await super().get_messages(chat, limit=limit, ids=ids)


class _FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError("bad status")


class _FakeHttpx:
    def __init__(self, text, status=200):
        self._text = text
        self._status = status

    async def get(self, url, **kwargs):
        return _FakeResp(self._text, self._status)


class _FakeReplyEvent:
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
        self.auto_respond = set()
        self.ignored_chats = set()
        self.ignored_users = set()
        self.seen_msg_keys = set()
        self.last_chat_activity = {}


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

    def test_get_chat_history_in_guard_and_ok(self):
        self.assertEqual(self._run("get_chat_history_in", {}), "Пустой chat.")
        out = self._run("get_chat_history_in", {"chat": "@x", "limit": 2})
        self.assertEqual(len(out.splitlines()), 2)

    def test_send_message_empty_and_ok(self):
        self.assertEqual(self._run("send_message", {}), "Пустой текст.")
        self.assertEqual(
            self._run("send_message", {"text": "hi"}), "Сообщение отправлено."
        )
        self.assertEqual(self.fake_client.sent[-1], (self.CHAT_ID, "hi"))

    def test_delete_message_bad_and_ok(self):
        self.assertEqual(
            self._run("delete_message", {"message_id": "x"}), "Некорректный message_id."
        )
        self.assertEqual(
            self._run("delete_message", {"message_id": 9}), "Сообщение удалено."
        )
        self.assertEqual(self.fake_client.deleted[-1], (self.CHAT_ID, [9]))

    def test_forward_message_bad_and_ok(self):
        self.assertEqual(
            self._run("forward_message", {"message_id": "x", "target": "@t"}),
            "Некорректный message_id.",
        )
        self.assertEqual(
            self._run("forward_message", {"message_id": 1, "target": ""}),
            "Пустой target.",
        )
        self.assertEqual(
            self._run("forward_message", {"message_id": 1, "target": "@t"}),
            "Сообщение переслано.",
        )
        self.assertEqual(self.fake_client.forwarded[-1], ("@t", 1, self.CHAT_ID))

    def test_create_poll_guards_and_ok(self):
        self.assertEqual(self._run("create_poll", {"question": ""}), "Пустой вопрос.")
        self.assertEqual(
            self._run("create_poll", {"question": "q", "options": ["a"]}),
            "Нужно минимум 2 варианта.",
        )
        self.assertEqual(
            self._run("create_poll", {"question": "q", "options": ["a", " ", ""]}),
            "Нужно минимум 2 непустых варианта.",
        )
        self.assertEqual(
            self._run("create_poll", {"question": "q", "options": ["a", "b"]}),
            "Опрос создан.",
        )
        self.assertEqual(len(self.fake_client.files), 1)

    def test_pin_unpin_get_pinned(self):
        self.assertEqual(
            self._run("pin_message", {"message_id": "x"}), "Некорректный message_id."
        )
        self.assertEqual(
            self._run("pin_message", {"message_id": 3, "notify": True}),
            "Сообщение закреплено.",
        )
        self.assertEqual(self.fake_client.pinned[-1], (self.CHAT_ID, 3, True))
        self.assertEqual(
            self._run("unpin_message", {"message_id": 3}), "Сообщение откреплено."
        )
        self.assertEqual(self.fake_client.unpinned[-1], (self.CHAT_ID, 3))
        self.assertEqual(self._run("get_pinned_messages", {}), "[5] 1: pinned")

    def test_react_to_message_guards_and_ok(self):
        self.assertEqual(
            self._run("react_to_message", {"message_id": "x", "emoji": "x"}),
            "Некорректный message_id.",
        )
        self.assertEqual(
            self._run("react_to_message", {"message_id": 1, "emoji": ""}),
            "Пустая реакция.",
        )
        self.assertEqual(
            self._run("react_to_message", {"message_id": 1, "emoji": "ok"}),
            "Реакция ok поставлена.",
        )
        self.assertEqual(len(self.fake_client.requests), 1)

    def test_last_and_search_messages(self):
        out = self._run("get_last_messages", {"limit": 2})
        self.assertEqual(len(out.splitlines()), 2)
        self.assertEqual(self._run("search_messages", {"query": ""}), "Пустой запрос.")
        out = self._run("search_messages", {"query": "t", "limit": 2})
        self.assertEqual(len(out.splitlines()), 2)

    def test_web_search_empty_and_ok(self):
        self.assertEqual(self._run("web_search", {"query": ""}), "Пустой запрос.")
        html = '<a class="result__a" href="https://ex.com">Title</a>'
        with mock.patch.object(
            tools_module, "_get_httpx_client", lambda: _FakeHttpx(html)
        ):
            out = self._run("web_search", {"query": "x"})
        self.assertIn("Title", out)
        self.assertIn("https://ex.com", out)

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
            self.assertEqual(self._run("fetch_url", {"url": "https://ex.com"}), "Hi")

    def test_run_subagent_guards(self):
        self.assertEqual(
            self._run("run_subagent", {"task": "x"}), "Субагенты недоступны."
        )


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

    def test_safe_reply_empty_and_success(self):
        event = _FakeReplyEvent()
        recent = set()
        sent = asyncio.run(core.safe_reply(event, "   ", 3, recent))
        self.assertEqual(event.replies, ["…"])
        if sent is None:
            self.fail("safe_reply returned None")
        self.assertEqual(sent.id, 77)
        self.assertIn(77, recent)

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
            set(),
            set(),
            set(),
            5,
            5,
            False,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("Контекст очищен. / Context cleared.", False, True))
        self.assertEqual(len(hist[1]), 0)

    def test_handle_command_state_model(self):
        overrides = {}
        resp = core.handle_command_state(
            ("model", "gpt"),
            1,
            True,
            {},
            overrides,
            set(),
            set(),
            set(),
            5,
            5,
            False,
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
            set(),
            set(),
            set(),
            5,
            5,
            False,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("Текущая модель / Current model: gpt", False, False))

    def test_handle_command_state_autorespond(self):
        auto = set()
        resp = core.handle_command_state(
            ("autorespond", True),
            1,
            True,
            {},
            {},
            auto,
            set(),
            set(),
            5,
            5,
            False,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("Авто-ответ ВКЛ. / Auto-reply ON.", True, False))
        self.assertIn(1, auto)
        resp = core.handle_command_state(
            ("autorespond", False),
            1,
            True,
            {},
            {},
            auto,
            set(),
            set(),
            5,
            5,
            False,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("Авто-ответ ВЫКЛ. / Auto-reply OFF.", True, False))
        self.assertNotIn(1, auto)

    def test_handle_command_state_auto_status(self):
        resp = core.handle_command_state(
            ("auto_status", None),
            1,
            True,
            {},
            {},
            set(),
            set(),
            set(),
            5,
            5,
            True,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("Авто-ответ / Auto-reply: ON", False, False))

    def test_handle_command_state_history(self):
        hist = {1: deque([{"role": "user", "content": "ab"}])}
        resp = core.handle_command_state(
            ("history", None),
            1,
            True,
            hist,
            {},
            set(),
            set(),
            set(),
            5,
            5,
            False,
            "m",
            [],
            "h",
        )
        if resp is None:
            self.fail("resp is None")
        self.assertIn("Messages in context: 1", resp[0])

    def test_handle_command_state_ping(self):
        resp = core.handle_command_state(
            ("ping", None),
            1,
            True,
            {},
            {},
            set(),
            set(),
            set(),
            5,
            5,
            False,
            "m",
            [],
            "h",
        )
        if resp is None:
            self.fail("resp is None")
        self.assertIn("Model: m", resp[0])

    def test_handle_command_state_models_and_help(self):
        resp = core.handle_command_state(
            ("models", None),
            1,
            True,
            {},
            {},
            set(),
            set(),
            set(),
            5,
            5,
            False,
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
            set(),
            set(),
            set(),
            5,
            5,
            False,
            "m",
            [],
            "h",
        )
        self.assertEqual(resp, ("h", False, False))

    def test_handle_command_state_ignore(self):
        ignored = set()
        resp = core.handle_command_state(
            ("ignore", None),
            1,
            True,
            {},
            {},
            set(),
            ignored,
            set(),
            5,
            5,
            False,
            "m",
            [],
            "h",
        )
        self.assertIn(1, ignored)
        if resp is None:
            self.fail("resp is None")
        self.assertTrue(resp[1])

    def test_handle_command_state_unknown(self):
        resp = core.handle_command_state(
            ("nope", None),
            1,
            True,
            {},
            {},
            set(),
            set(),
            set(),
            5,
            5,
            False,
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
        _hist, messages = asyncio.run(
            core.prepare_messages(store, 1, 5, 5, lambda cid: "sys")
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

        async def edit_fn(chat_id, msg_id, text):
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
                None,
                1,
                True,
                [],
                "m",
                "p:",
                5,
                render,
                edit_fn,
                reply_fn,
                _NullAsyncContext(),
                stream_fn,
                None,
                None,
                0.0,
            )
        )
        self.assertEqual(answer, "hi")
        self.assertEqual(edited[-1], "p:hi")

    def test_parse_state_data(self):
        data = {
            "model_overrides": {"1": "m"},
            "auto_respond": [1],
            "ignored_chats": [2],
            "ignored_users": [3],
        }
        parsed = core.parse_state_data(data)
        self.assertEqual(parsed["model_overrides"], {1: "m"})
        self.assertEqual(parsed["auto_respond"], {1})


class VisibilityCommandsTest(BotTestCase):
    CHAT_ID = 7591254790

    def test_aliases_parse(self):
        self.assertEqual(
            userbot.handle_commands(".db reasoning off"), ("reasoning", False)
        )
        self.assertEqual(userbot.handle_commands(".db tools on"), ("tools", True))
        self.assertEqual(
            userbot.handle_commands(".db ризонинг выкл"), ("reasoning", False)
        )
        self.assertEqual(
            userbot.handle_commands(".db инструменты"), ("tools_status", None)
        )
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
                "auto_respond": set(),
                "ignored_chats": set(),
                "ignored_users": set(),
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

    def test_aliases_parse(self):
        self.assertEqual(userbot.handle_commands(".db coder on"), ("coder", True))
        self.assertEqual(userbot.handle_commands(".db кодер выкл"), ("coder", False))
        self.assertEqual(userbot.handle_commands(".db coder"), ("coder_status", None))
        self.assertEqual(bot.handle_bot_commands("/coder off"), ("coder", False))
        self.assertEqual(bot.handle_bot_commands("/coder"), ("coder_status", None))

    def test_coder_tools_are_agentic(self):
        names = {t["function"]["name"] for t in tools_module.CODER_TOOLS}
        self.assertEqual(
            names,
            {
                "read_file",
                "write_file",
                "edit_file",
                "list_dir",
                "search_files",
                "run_shell",
                "web_search",
                "fetch_url",
                "get_time",
            },
        )
        telegram_names = {
            "send_message",
            "edit_message",
            "delete_message",
            "forward_message",
            "pin_message",
            "create_poll",
            "list_chats",
            "react_to_message",
        }
        self.assertFalse(names & telegram_names)

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
        source = Path("userbot.py").read_text(encoding="utf-8")
        self.assertNotIn("CODER_TOOLS if", source)
        self.assertNotIn('mode="coder"', source)

    def test_menu_covers_every_help_command(self):
        source = Path("bot.py").read_text(encoding="utf-8")
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
        for name in ("coder", "reasoning", "tools"):
            self.assertIn(name, menu)

    def test_bot_handler_uses_coder_tools(self):
        source = Path("bot.py").read_text(encoding="utf-8")
        self.assertIn("tools_module.CODER_TOOLS if coder_active else userbot.TOOLS", source)
        self.assertIn('mode = "coder" if coder_active else "bot"', source)

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
        written = asyncio.run(
            userbot.execute_tool(
                "write_file", {"path": target, "content": "alpha\nbeta\n"}, -100
            )
        )
        self.assertIn("Создан", written)
        read = asyncio.run(userbot.execute_tool("read_file", {"path": target}, -100))
        self.assertIn("1|alpha", read)
        edited = asyncio.run(
            userbot.execute_tool(
                "edit_file",
                {"path": target, "old_string": "beta", "new_string": "gamma"},
                -100,
            )
        )
        self.assertIn("Изменён", edited)
        found = asyncio.run(
            userbot.execute_tool(
                "search_files", {"pattern": "gamma", "path": ".coder_selftest"}, -100
            )
        )
        self.assertIn("note.txt:2", found)
        listed = asyncio.run(
            userbot.execute_tool("list_dir", {"path": ".coder_selftest"}, -100)
        )
        self.assertIn("note.txt", listed)
        missing = asyncio.run(
            userbot.execute_tool("read_file", {"path": "/etc/passwd"}, -100)
        )
        self.assertIn("вне разрешённого корня", missing)

    def test_creator_info_exposed(self):
        self.assertIn("Создатель", userbot.CREATOR_INFO)
        self.assertTrue(userbot.CREATOR_ID)


class UserbotHelpersTest(BotTestCase):
    def test_register_sender_and_is_unrestricted(self):
        saved = userbot.OWNER_IDS
        userbot.OWNER_IDS = {42}
        self.addCleanup(setattr, userbot, "OWNER_IDS", saved)
        self.assertTrue(userbot.register_sender(42, -1))
        self.assertTrue(userbot.is_unrestricted(-1))
        self.assertFalse(userbot.register_sender(7, -2))
        self.assertFalse(userbot.is_unrestricted(-2))
        self.assertFalse(userbot.register_sender(None, -3))

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
        self.assertIn(userbot.CREATOR_INFO, userbot.system_for(1))
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
                "main,bot,userbot,proxies,core",
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
            "--source=bot,userbot,proxies,tools,core",
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
