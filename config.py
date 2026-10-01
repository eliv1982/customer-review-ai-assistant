"""
Загрузка настроек приложения из переменных окружения.

Используется python-dotenv: при разработке значения подхватываются из файла .env.
"""
from __future__ import annotations

import os
import re
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


def parse_allowed_telegram_ids(value: str | None) -> frozenset[int]:
    """
    Разбирает ``ALLOWED_TELEGRAM_IDS``: числовые ID через запятую и/или пробелы.

    Незаданное, пустое или состоящее только из пробелов значение даёт пустое множество —
    ограничение выключено. Любой другой токен (не положительное целое) — ``ValueError``:
    опечатка не должна молча превратиться в пустой список и снять ограничение. По той же
    причине непустое значение без единого ID (например, ``","``) — тоже ``ValueError``.
    """
    raw = (value or "").strip()
    if not raw:
        return frozenset()

    ids: set[int] = set()
    for token in re.split(r"[,\s]+", raw):
        if not token:
            continue
        # ID пользователя Telegram — положительное целое; 0 зарезервирован под «отправителя нет».
        if not re.fullmatch(r"[0-9]+", token) or int(token) == 0:
            raise ValueError(
                f"ALLOWED_TELEGRAM_IDS: {token!r} не похож на ID пользователя Telegram; "
                "ожидаются положительные целые числа через запятую"
            )
        ids.add(int(token))
    if not ids:
        raise ValueError(
            f"ALLOWED_TELEGRAM_IDS: значение {value!r} не содержит ни одного ID; "
            "оставьте переменную пустой, чтобы снять ограничение, или укажите ID через запятую"
        )
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    """Неизменяемый снимок конфигурации (секреты могут быть None, если не заданы)."""

    telegram_bot_token: str | None
    openai_api_key: str | None
    database_path: str
    openai_model: str
    log_level: str
    # Пусто — бот открыт для всех; иначе им пользуются только перечисленные ID.
    allowed_telegram_ids: frozenset[int] = frozenset()


def get_settings() -> Settings:
    """Собирает настройки из текущего окружения."""
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        database_path=resolve_database_path(os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH)),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        allowed_telegram_ids=parse_allowed_telegram_ids(os.getenv("ALLOWED_TELEGRAM_IDS")),
    )
