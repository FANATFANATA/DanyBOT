import asyncio
import contextlib
import logging
import signal

import bot
import subagents
import userbot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("danybot.main")


async def _flush_savers():
    with contextlib.suppress(Exception):
        await userbot.HISTORY_SAVER.flush()
    with contextlib.suppress(Exception):
        await bot.HISTORY_SAVER.flush()


async def run():
    userbot.load_state()
    userbot.load_history()
    await userbot.refresh_models()

    subagents.configure(
        ai=userbot.ai,
        model=userbot.SUBAGENT_MODEL or userbot.DANYAPI_MODEL,
        verifier=userbot.verify_tool_call,
        stats=userbot._bot_stats,
        max_rounds=userbot.SUBAGENT_MAX_ROUNDS,
        max_tokens=userbot.MAX_TOKENS,
        concurrency=userbot.SUBAGENT_CONCURRENCY,
        enabled=userbot.SUBAGENT_ENABLED,
        timeout=userbot.REQUEST_TIMEOUT,
    )

    tasks = []
    if userbot.ENABLE_USERBOT:
        tasks.append(asyncio.create_task(userbot.start_userbot()))
    if userbot.ENABLE_BOT:
        tasks.append(asyncio.create_task(bot.start_bot()))
    if not tasks:
        logger.error("Не включён ни один режим: ENABLE_USERBOT/ENABLE_BOT")
        await _flush_savers()
        return

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(*_args):
        if not stop.is_set():
            logger.info("Остановка / Stopping...")
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, request_stop)

    stop_task = asyncio.create_task(stop.wait())
    all_tasks = [*tasks, stop_task]

    try:
        while True:
            done, _pending = await asyncio.wait(
                all_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                break
            mode_tasks = [t for t in tasks if t in done]
            for task in mode_tasks:
                exc = task.exception()
                if exc is not None:
                    logger.error("Режим завершился с ошибкой: %r", exc)
                else:
                    logger.warning("Режим завершился: %s", task.get_name())
            if all(t.done() for t in tasks):
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for task in all_tasks:
            if not task.done():
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*all_tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await userbot.disconnect_quietly()
        with contextlib.suppress(Exception):
            await bot.disconnect_quietly()
        await _flush_savers()
        logger.info("Завершено / Stopped.")


def main():
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
