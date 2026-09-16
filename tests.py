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
import proxies
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
        self.assertIn("💭 «<i>думаю дальше</i>» 💭", text)
        self.assertIn("🔧", text)
        self.assertIn("<code>web_search</code> <code>fetch_url</code>", text)
        self.assertIn("💬 «<b>ответ текста</b>» 💬", text)
        self.assertEqual(text.count("web_search"), 1)

    def test_escapes_html(self):
        text = userbot.render_response("", ["a<b"], ["x&y"], "c<d>e")
        self.assertIn("a&lt;b", text)
        self.assertIn("x&amp;y", text)
        self.assertIn("<b>c&lt;d&gt;e</b>", text)

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
        import tools

        self.assertIs(userbot.TOOLS, tools.TOOLS)

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
        self.assertEqual(fake_ai.chat.completions.calls[0]["model"], "m")
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
            cmd += [*PY_FILES]
        elif name == "pylint":
            cmd += [
                "--disable=all",
                "--enable=F,E,W",
                "--disable=W0603,W0212,W0613,W0621",
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
