import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, cast

import httpx
from telethon import TelegramClient
from telethon.errors import InvalidChecksumError, RPCError
from telethon.errors.common import ReadCancelledError
from telethon.sessions import MemorySession
from telethon.tl.functions.help import GetConfigRequest

logger = logging.getLogger("danybot.proxy")

CACHE_FILE = Path(__file__).parent / "working_proxies.json"
RAW_CACHE_FILE = Path(__file__).parent / "proxy_cache.txt"

PROTOCOLS = {"socks5", "socks4", "http"}

VALIDATE_API_ID = 2040
VALIDATE_API_HASH = "b18441a1ff607e10a989891a5462e627"

VALIDATION_ERRORS = (
    RPCError,
    ConnectionError,
    OSError,
    TimeoutError,
    BufferError,
    ValueError,
    TypeError,
    InvalidChecksumError,
    ReadCancelledError,
)

SOURCES = [
    (
        "socks5",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    ),
    (
        "socks4",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
    ),
    (
        "http",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    ),
    (
        "socks5",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    ),
    (
        "socks4",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt",
    ),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    (
        "socks5",
        "https://cdn.jsdelivr.net/gh/roosterkid/openproxylist@main/SOCKS5_RAW.txt",
    ),
    (
        "socks4",
        "https://cdn.jsdelivr.net/gh/roosterkid/openproxylist@main/SOCKS4_RAW.txt",
    ),
    ("http", "https://cdn.jsdelivr.net/gh/roosterkid/openproxylist@main/HTTPS_RAW.txt"),
    (
        "socks5",
        "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=10000&country=all",
    ),
    (
        "socks4",
        "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4&timeout=10000&country=all",
    ),
    (
        "http",
        "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    ),
]

IPPORT_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})")


def parse_proxy_lines(text: str, protocol: str):
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = IPPORT_RE.search(line)
        if not m:
            continue
        host = m.group(1)
        port = int(m.group(2))
        if 1 <= port <= 65535:
            result.append((protocol, host, port))
    return result


def load_raw_cache():
    if not RAW_CACHE_FILE.exists():
        return []
    proxies = []
    for line in RAW_CACHE_FILE.read_text(
        encoding="utf-8", errors="ignore"
    ).splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 3:
            proto, host, port = parts[0], parts[1], parts[2]
            if proto in PROTOCOLS and port.isdigit():
                proxies.append((proto, host, int(port)))
    return proxies


def save_raw_cache(proxies):
    lines = [f"{p} {h} {port}" for p, h, port in proxies]
    RAW_CACHE_FILE.write_text("\n".join(lines), encoding="utf-8")


async def fetch_sources():
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:

        async def fetch_one(protocol: str, url: str):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                return parse_proxy_lines(resp.text, protocol)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                logger.debug("Не удалось скачать %s: %s", url, exc)
                return []

        results = await asyncio.gather(
            *(fetch_one(protocol, url) for protocol, url in SOURCES)
        )
        proxies = [item for chunk in results for item in chunk]
    logger.info("Собрано %d прокси из %d источников", len(proxies), len(SOURCES))
    return proxies


def dedupe(proxies):
    seen = set()
    out = []
    for p in proxies:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def proxy_to_telethon(item):
    if not item:
        return None
    protocol, host, port = item
    return {"proxy_type": protocol, "addr": host, "port": port}


def telethon_to_item(proxy_dict):
    if not proxy_dict:
        return None
    return (proxy_dict["proxy_type"], proxy_dict["addr"], proxy_dict["port"])


async def validate_one_mtproto(proxy_dict, timeout=12):
    tmp = TelegramClient(
        MemorySession(),
        VALIDATE_API_ID,
        VALIDATE_API_HASH,
        proxy=proxy_dict,
        timeout=10,
        connection_retries=1,
        request_retries=1,
    )
    try:
        await asyncio.wait_for(tmp.connect(), timeout=timeout)
        await asyncio.wait_for(tmp(GetConfigRequest()), timeout=timeout)
        return True
    except VALIDATION_ERRORS:
        return False
    finally:
        with contextlib.suppress(Exception):
            await cast(Any, tmp.disconnect())


async def validate_one(proto_name, host, port, timeout=12):
    proxy_dict = {
        "proxy_type": proto_name,
        "addr": host,
        "port": port,
    }
    return await validate_one_mtproto(proxy_dict, timeout=timeout)


async def validate_many(proxies, limit=10, concurrency=20):
    working = []
    sem = asyncio.Semaphore(concurrency)

    async def run(item):
        async with sem:
            if await validate_one(*item):
                return item
            return None

    tasks = [asyncio.create_task(run(p)) for p in proxies]
    try:
        for coro in asyncio.as_completed(tasks):
            res = await cast(Any, coro)
            if res:
                working.append(res)
                logger.info("Рабочий прокси: %s %s:%s", *res)
                if len(working) >= limit:
                    break
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return working


def load_proxy_cache():
    if not CACHE_FILE.exists():
        return []
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return [(p["protocol"], p["host"], p["port"]) for p in data]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save_proxy_cache(proxies):
    data = [{"protocol": p, "host": h, "port": port} for p, h, port in proxies]
    CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def get_working_proxies(limit=10, prefer_protocol="socks5"):
    cached = load_proxy_cache()
    if cached:
        valid = await validate_many(cached[:30], limit=limit, concurrency=20)
        if valid:
            return valid

    logger.info("Скачиваю списки прокси...")
    fetched = await fetch_sources()
    raw_cached = load_raw_cache()
    all_proxies = dedupe(fetched + raw_cached)
    if prefer_protocol:
        all_proxies.sort(key=lambda p: p[0] != prefer_protocol)
    if all_proxies:
        save_raw_cache(all_proxies[:5000])

    logger.info("Прокси в пуле: %d", len(all_proxies))
    working = await validate_many(all_proxies, limit=limit)
    if working:
        save_proxy_cache(working)
        return working

    return []


async def get_working_proxy(prefer_protocol="socks5"):
    working = await get_working_proxies(limit=1, prefer_protocol=prefer_protocol)
    return working[0] if working else None


def mark_bad_proxy(item):
    if not item:
        return
    _protocol, host, port = item
    cached = load_proxy_cache()
    cached = [p for p in cached if not (p[1] == host and p[2] == port)]
    save_proxy_cache(cached)
    logger.info("Удалил из кэша нерабочий прокси: %s:%s", host, port)


async def get_proxy_candidates(limit=40):
    candidates = []
    host = os.getenv("PROXY_HOST", "").strip()
    port = os.getenv("PROXY_PORT", "").strip()
    proto = os.getenv("PROXY_TYPE", "socks5").strip().lower()

    if host and port.isdigit():
        candidates.append({"proxy_type": proto, "addr": host, "port": int(port)})

    if os.getenv("PROXY_AUTO", "1").strip() not in ("0", "false", "no", ""):
        items = await get_working_proxies(limit=limit, prefer_protocol=proto)
        for item in items:
            d = proxy_to_telethon(item)
            if d and d not in candidates:
                candidates.append(d)

    return candidates


async def get_proxy():
    c = await get_proxy_candidates(limit=5)
    return c[0] if c else None


async def main():
    item = await get_working_proxy()
    if item:
        print("Найден рабочий прокси:", item)
    else:
        print("Рабочих прокси не найдено")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    asyncio.run(main())
