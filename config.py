"""
Загрузка настроек приложения из переменных окружения.

Используется python-dotenv: при разработке значения подхватываются из файла .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Подгружаем .env в os.environ до чтения настроек.
load_dotenv()

# Корень проекта: единая базовая директория для относительных путей (не текущий cwd процесса).
PROJECT_ROOT: Path = Path(__file__).resolve().parent

DEFAULT_DATABASE_PATH = "data/reviews.db"


def resolve_database_path(value: str) -> str:
    """
    Путь к SQLite: абсолютный остаётся как есть, относительный отсчитывается от ``PROJECT_ROOT``.

    Результат не зависит от рабочего каталога, из которого запущен процесс, поэтому запуск
    из другого cwd не создаёт вторую базу.
    """
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(PROJECT_ROOT / path)


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
        database_path=resolve_database_path(os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH)),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
