import asyncio
import contextlib
import logging
import signal

import bot
import userbot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("danybot.main")


async def run():
    userbot.load_state()
    userbot.load_history()
    await userbot.refresh_models()

    tasks = []
    if userbot.ENABLE_USERBOT:
        tasks.append(asyncio.create_task(userbot.start_userbot()))
    if userbot.ENABLE_BOT:
        tasks.append(asyncio.create_task(bot.start_bot()))
    if not tasks:
        logger.error("Не включён ни один режим: ENABLE_USERBOT/ENABLE_BOT")
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
        _done, pending = await asyncio.wait(
            all_tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*pending, return_exceptions=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            await userbot.disconnect_quietly()
        with contextlib.suppress(Exception):
            await bot.disconnect_quietly()
        for task in all_tasks:
            if not task.done():
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*all_tasks, return_exceptions=True)
        logger.info("Завершено / Stopped.")


def main():
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
