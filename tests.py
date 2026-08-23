import argparse
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

import bot
import proxies

PROJECT_DIR = Path(__file__).resolve().parent
PY_FILES = ("bot.py", "proxies.py", "tests.py")
WHITELIST_FILE = "vulture_whitelist.py"
BANDIT_SKIP = "B404,B603"
VULTURE_IGNORE_NAMES = "test_*,setUp"


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
        (bot, "STATE_FILE", "state.json"),
        (bot, "HISTORY_FILE", "history.json"),
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
        "model_overrides": dict(bot.model_overrides),
        "auto_respond": set(bot.auto_respond),
        "ignored_chats": set(bot.ignored_chats),
        "ignored_users": set(bot.ignored_users),
        "chat_history": {
            k: deque(v, maxlen=v.maxlen) for k, v in bot.chat_history.items()
        },
        "MODELS": list(bot.MODELS),
    }


def restore_bot_state(snap):
    bot.model_overrides = snap["model_overrides"]
    bot.auto_respond = snap["auto_respond"]
    bot.ignored_chats = snap["ignored_chats"]
    bot.ignored_users = snap["ignored_users"]
    bot.chat_history = snap["chat_history"]
    bot.MODELS = snap["MODELS"]


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

    async def edit_message(self, chat, msg_id, text):
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
            self.assertEqual(bot._env_int("DANYBOT_TEST_INT", 7), 7)

    def test_env_int_valid_and_garbage(self):
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_INT": "42"}):
            self.assertEqual(bot._env_int("DANYBOT_TEST_INT", 7), 42)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_INT": "abc"}):
            self.assertEqual(bot._env_int("DANYBOT_TEST_INT", 7), 7)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_INT": "3.9"}):
            self.assertEqual(bot._env_int("DANYBOT_TEST_INT", 7), 7)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_INT": "-5"}):
            self.assertEqual(bot._env_int("DANYBOT_TEST_INT", 7), -5)

    def test_env_float_valid_garbage_empty(self):
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_FLT": "2.5"}):
            self.assertEqual(bot._env_float("DANYBOT_TEST_FLT", 1.0), 2.5)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_FLT": "junk"}):
            self.assertEqual(bot._env_float("DANYBOT_TEST_FLT", 1.0), 1.0)
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_FLT": ""}):
            self.assertEqual(bot._env_float("DANYBOT_TEST_FLT", 1.0), 1.0)

    def test_env_str_strips_and_defaults(self):
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_STR": "  hi  "}):
            self.assertEqual(bot._env_str("DANYBOT_TEST_STR", "d"), "hi")
        with mock.patch.dict(os.environ, {"DANYBOT_TEST_STR": "   "}):
            self.assertEqual(bot._env_str("DANYBOT_TEST_STR", "d"), "")
        with mock.patch.dict(os.environ):
            os.environ.pop("DANYBOT_TEST_STR", None)
            self.assertEqual(bot._env_str("DANYBOT_TEST_STR", "d"), "d")


class MakeSessionTest(unittest.TestCase):
    def test_plain_name_passthrough(self):
        self.assertEqual(bot.make_session("plainname"), "plainname")

    def test_valid_string_session(self):
        session = StringSession()
        session.set_dc(2, "149.154.167.50", 443)
        session.auth_key = AuthKey(bytes(256))
        raw = session.save()
        result = bot.make_session(raw)
        self.assertIsInstance(result, StringSession)

    def test_struct_error_fallback(self):
        self.assertEqual(bot.make_session("1AAAA"), "1AAAA")

    def test_binascii_error_fallback(self):
        self.assertEqual(bot.make_session("1notvalid@@@"), "1notvalid@@@")


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
                self.assertIsNotNone(bot.TRIGGER_RE.search(case))

    def test_no_match(self):
        for case in self.NO_MATCH_CASES:
            with self.subTest(case=case):
                self.assertIsNone(bot.TRIGGER_RE.search(case))


class HandleCommandsTest(unittest.TestCase):
    CHECKS = (
        (".db reset", ("reset", None)),
        ("  .DB RESET ", ("reset", None)),
        (".db reset extra args", ("reset", None)),
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
        ".unknown reset",
    )

    def test_commands_parsed(self):
        for text, expected in self.CHECKS:
            with self.subTest(text=text):
                self.assertEqual(bot.handle_commands(text), expected)

    def test_commands_none(self):
        for text in self.NONE_CASES:
            with self.subTest(text=text):
                self.assertIsNone(bot.handle_commands(text))


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
                self.assertEqual(bot.strip_role_tag(src), expected)


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
                self.assertEqual(bot.safe_eval(expr), expected)

    def test_errors(self):
        for expr in self.ERROR_CASES:
            with self.subTest(expr=expr):
                result = bot.safe_eval(expr)
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
        bot.model_overrides = {123: "model-a", -456: "model-b"}
        bot.auto_respond = {1, -2}
        bot.ignored_chats = {-100}
        bot.ignored_users = {777}
        bot.save_state()
        bot.model_overrides = {}
        bot.auto_respond = set()
        bot.ignored_chats = set()
        bot.ignored_users = set()
        bot.load_state()
        self.assertEqual(bot.model_overrides, {123: "model-a", -456: "model-b"})
        self.assertEqual(bot.auto_respond, {1, -2})
        self.assertEqual(bot.ignored_chats, {-100})
        self.assertEqual(bot.ignored_users, {777})

    def test_corrupt_json_tolerated(self):
        bot.STATE_FILE.write_text("{not json", encoding="utf-8")
        bot.model_overrides = {}
        bot.load_state()
        self.assertEqual(bot.model_overrides, {})
        self.assertEqual(bot.auto_respond, set())

    def test_wrong_structure_tolerated(self):
        bot.STATE_FILE.write_text('{"model_overrides": [1, 2]}', encoding="utf-8")
        bot.model_overrides = {}
        bot.load_state()
        self.assertEqual(bot.model_overrides, {})


class HistoryRoundtripTest(BotTestCase):
    def test_roundtrip_and_maxlen(self):
        dm_msgs = [{"role": "user", "content": f"m{i}"} for i in range(3)]
        group_msgs = [{"role": "assistant", "content": "g"}]
        bot.chat_history = {
            111: deque(dm_msgs, maxlen=bot.DM_HISTORY_LIMIT),
            -222: deque(group_msgs, maxlen=bot.GROUP_HISTORY_LIMIT),
        }
        bot.save_history()
        bot.chat_history = {}
        bot.load_history()
        self.assertEqual(list(bot.chat_history[111]), dm_msgs)
        self.assertEqual(bot.chat_history[111].maxlen, bot.DM_HISTORY_LIMIT)
        self.assertEqual(list(bot.chat_history[-222]), group_msgs)
        self.assertEqual(bot.chat_history[-222].maxlen, bot.GROUP_HISTORY_LIMIT)

    def test_bad_keys_skipped(self):
        bot.HISTORY_FILE.write_text(
            json.dumps({"abc": [], "111": [{"role": "user", "content": "x"}]}),
            encoding="utf-8",
        )
        bot.load_history()
        self.assertNotIn("abc", {str(k) for k in bot.chat_history})
        self.assertIn(111, bot.chat_history)

    def test_corrupt_tolerated(self):
        bot.HISTORY_FILE.write_text("[[[broken", encoding="utf-8")
        bot.chat_history = {}
        bot.load_history()
        self.assertEqual(bot.chat_history, {})


class ModelsTextTest(BotTestCase):
    def test_default_model_marked_once(self):
        bot.MODELS = ["deepseek-v4-flash"]
        bot.model_overrides = {}
        text = bot.models_text(-100)
        self.assertEqual(text.count("(текущая)"), 1)
        self.assertIn("deepseek-v4-flash", text)

    def test_override_appended(self):
        bot.MODELS = ["base-model"]
        bot.model_overrides = {-100: "custom-model"}
        text = bot.models_text(-100)
        self.assertEqual(text.count("(текущая)"), 1)
        self.assertIn("custom-model (текущая)", text)
        self.assertIn("• base-model\n", text)


class ExecuteToolTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.fake_client = FakeClient()
        self._orig_client = bot.client
        bot.client = self.fake_client
        self.addCleanup(setattr, bot, "client", self._orig_client)

    def test_evaluate(self):
        result = asyncio.run(bot.execute_tool("evaluate", {"expression": "2+2"}, -100))
        self.assertEqual(result, "4")

    def test_unknown_tool(self):
        result = asyncio.run(bot.execute_tool("nope", {}, -100))
        self.assertEqual(result, "Неизвестная функция: nope")

    def test_send_message_to_guards(self):
        empty = asyncio.run(bot.execute_tool("send_message_to", {}, -100))
        self.assertEqual(empty, "Нужны chat и text.")
        ok = asyncio.run(
            bot.execute_tool("send_message_to", {"chat": "@me", "text": "hello"}, -100)
        )
        self.assertEqual(ok, "Сообщение отправлено.")
        self.assertEqual(self.fake_client.sent, [("@me", "hello")])

    def test_edit_message_guard_and_call(self):
        empty = asyncio.run(
            bot.execute_tool("edit_message", {"message_id": 1, "text": ""}, -100)
        )
        self.assertEqual(empty, "Пустой текст.")
        ok = asyncio.run(
            bot.execute_tool("edit_message", {"message_id": 5, "text": "new"}, -100)
        )
        self.assertEqual(ok, "Сообщение отредактировано.")
        self.assertEqual(self.fake_client.edited, [(-100, 5, "new")])

    def test_get_chat_history_formatting_and_clamp(self):
        result = asyncio.run(bot.execute_tool("get_chat_history", {"limit": 150}, -100))
        self.assertEqual(self.fake_client.history_limits[-1], 100)
        lines = result.splitlines()
        self.assertEqual(len(lines), 100)
        self.assertEqual(lines[0], "[1] 1: t1")
        self.assertEqual(lines[-1], "[100] 1: t100")

    def test_get_message_by_id_found_and_missing(self):
        found = asyncio.run(
            bot.execute_tool("get_message_by_id", {"message_id": 42}, -100)
        )
        self.assertEqual(found, "found")
        missing = asyncio.run(
            bot.execute_tool("get_message_by_id", {"message_id": 43}, -100)
        )
        self.assertEqual(missing, "Сообщение не найдено.")

    def test_get_chat_info_json(self):
        result = asyncio.run(bot.execute_tool("get_chat_info", {}, -100))
        data = json.loads(result)
        self.assertEqual(data["id"], 7)
        self.assertEqual(data["username"], "uchat")
        self.assertEqual(data["members"], 11)

    def test_get_user_info(self):
        result = asyncio.run(
            bot.execute_tool("get_user_info", {"handle": "@somebody"}, -100)
        )
        data = json.loads(result)
        self.assertEqual(data["name"], "A B")
        self.assertEqual(data["id"], 7)

    def test_get_user_info_empty_handle(self):
        result = asyncio.run(bot.execute_tool("get_user_info", {"handle": ""}, -100))
        self.assertEqual(result, "Пустой handle.")

    def test_list_chats_labels(self):
        result = asyncio.run(bot.execute_tool("list_chats", {"limit": 3}, -100))
        lines = result.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("1: name1 (@user1)"))

    def test_get_profile_json(self):
        result = asyncio.run(bot.execute_tool("get_profile", {}, -100))
        data = json.loads(result)
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["username"], "meuser")


class StreamToolsTest(BotTestCase):
    CHAT_ID = -100

    def setUp(self):
        super().setUp()
        self.tool_calls_made = []

        async def fake_execute(name, args, chat_id):
            self.tool_calls_made.append((name, args, chat_id))
            return "TOOLOK"

        self._orig_execute = bot.execute_tool
        self._orig_ai = bot.ai
        bot.execute_tool = fake_execute
        self.addCleanup(setattr, bot, "execute_tool", self._orig_execute)
        self.addCleanup(setattr, bot, "ai", self._orig_ai)

    def install_ai(self, rounds):
        fake_ai = FakeAI(rounds)
        bot.ai = fake_ai
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
            bot.stream_with_tools(
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
        self.assertEqual(kwargs["tools"], bot.TOOLS)
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
            bot.stream_with_tools(
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
            bot.stream_with_tools(
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

    def test_tool_round_limit_reached(self):
        endless = [
            make_chunk(make_delta(tool_calls=[make_tc(tc_id="c1", name="evaluate")]))
        ]
        self.install_ai([endless, endless])
        with mock.patch.object(bot, "MAX_TOOL_ROUNDS", 2):
            answer = asyncio.run(
                bot.stream_with_tools(
                    [],
                    "m",
                    self.CHAT_ID,
                    lambda p: self.collect([], p),
                    lambda p: self.collect([], p),
                )
            )
        self.assertEqual(answer, "Достигнут лимит циклов инструментов.")


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
        sent = asyncio.run(bot.safe_reply(event, "текст"))
        if sent is None or sent.id != 777:
            self.fail("safe_reply должен вернуть сообщение с id 777")
        self.assertEqual(attempts["n"], 3)
        self.assertIn(777, bot.recent_reply_ids)

    def test_safe_reply_bounded_attempts(self):
        event, attempts = self.make_event(["flood"])
        sent = asyncio.run(bot.safe_reply(event, "текст"))
        self.assertIsNone(sent)
        self.assertEqual(attempts["n"], bot.REPLY_ATTEMPTS)

    def test_safe_reply_rpc_fail_fast(self):
        event, attempts = self.make_event(["rpc"])
        sent = asyncio.run(bot.safe_reply(event, "текст"))
        self.assertIsNone(sent)
        self.assertEqual(attempts["n"], 1)


class EditRetryTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.flaky = FlakyEditClient()
        self._orig_client = bot.client
        bot.client = self.flaky
        self.addCleanup(setattr, bot, "client", self._orig_client)

    def test_edit_text_recovers_after_floods(self):
        ok = asyncio.run(bot.edit_text(-100, 5, "новый текст"))
        self.assertTrue(ok)
        self.assertEqual(self.flaky.attempts, 3)

    def test_edit_text_bounded(self):
        self.flaky.always_flood = True
        ok = asyncio.run(bot.edit_text(-100, 5, "новый текст"))
        self.assertFalse(ok)
        self.assertEqual(self.flaky.attempts, bot.REPLY_ATTEMPTS)


class FlakyEditClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.attempts = 0
        self.always_flood = False

    async def edit_message(self, chat, msg_id, text):
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
                "--disable=W0603,W0212,W0613",
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
                "bot,proxies",
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
    subprocess.run([*base, "erase"], cwd=PROJECT_DIR, capture_output=True, check=False)
    run_proc = subprocess.run(
        [
            *base,
            "run",
            "--source=bot,proxies",
            "-m",
            "unittest",
            "discover",
            "-s",
            ".",
            "-p",
            "tests.py",
        ],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if run_proc.returncode != 0:
        tail = (run_proc.stdout + run_proc.stderr).strip().splitlines()
        return ("coverage", "FAIL", " | ".join(tail[-2:])[:120])
    rep = subprocess.run(
        [*base, "report"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    note = extract_note("coverage", rep)
    return ("coverage", "PASS", note)


def run_linters():
    results = []
    for name, cmd in build_linters():
        if name == "coverage":
            if find_spec_or_none("coverage"):
                results.append(run_coverage())
            else:
                results.append((name, "SKIP", "инструмент не найден"))
            continue
        if cmd is None:
            results.append((name, "SKIP", "инструмент не найден"))
            continue
        proc = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode == 0:
            results.append((name, "PASS", extract_note(name, proc)))
            continue
        combined = proc.stdout + proc.stderr
        if name == "pip-audit" and (
            "Traceback" in combined or "Connection" in combined
        ):
            results.append((name, "SKIP", "нет доступа к сети или реестру"))
            continue
        tail = combined.strip().splitlines()
        note = " | ".join(tail[-3:])[:120]
        results.append((name, "FAIL", note))
    return results


def print_summary(unit_result, lint_results):
    total = unit_result.testsRun
    failures = len(unit_result.failures)
    errors = len(unit_result.errors)
    skipped = len(unit_result.skipped)
    print()
    print("=" * 64)
    print("СВОДКА")
    print("=" * 64)
    print(
        f"Юнит-тесты : {total - failures - errors - skipped}/{total} OK, "
        f"fail={failures}, error={errors}, skip={skipped}"
    )
    for name, status, note in lint_results:
        line = f"{name:<13} {status:<5}"
        if note:
            line += f"  {note}"
        print(line)
    print("=" * 64)
    lint_failed = any(status == "FAIL" for _, status, _ in lint_results)
    verdict_ok = unit_result.wasSuccessful() and not lint_failed
    print("ВЕРДИКТ   :", "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ" if verdict_ok else "ЕСТЬ ПРОВАЛЫ")
    return verdict_ok


def main(argv=None):
    _reconfigure_stdio()
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


if __name__ == "__main__":
    sys.exit(main())
