"""
Запуск Telegram-бота (long polling, без webhook).
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router

from bot.handlers import setup_handlers
from config import Settings

logger = logging.getLogger(__name__)


async def run_bot_polling(settings: Settings) -> None:
    """Блокирующий цикл long polling до остановки процесса (Ctrl+C)."""
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN пустой")

    bot = Bot(token=token)
    dp = Dispatcher()
    router = Router()
    setup_handlers(router, settings)
    dp.include_router(router)

    logger.info("Telegram-бот: запуск long polling (Ctrl+C для остановки)")
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        # Штатная остановка polling при завершении приложения.
        logger.info("Telegram-бот остановлен")
    finally:
        await bot.session.close()
        logger.info("Telegram-бот: polling остановлен")
