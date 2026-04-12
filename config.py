"""
Загрузка настроек приложения из переменных окружения.

Используется python-dotenv: при разработке значения подхватываются из файла .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Подгружаем .env в os.environ до чтения настроек.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Неизменяемый снимок конфигурации (секреты могут быть None, если не заданы)."""

    telegram_bot_token: str | None
    openai_api_key: str | None
    database_path: str
    openai_model: str
    log_level: str


def get_settings() -> Settings:
    """Собирает настройки из текущего окружения."""
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        database_path=os.getenv("DATABASE_PATH", "data/reviews.db"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
