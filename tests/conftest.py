"""Общие фикстуры для pytest."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram import Router

from bot.handlers import setup_handlers
from config import Settings
from services.review_service import init_database


@pytest.fixture
def tmp_db(tmp_path):
    """Пустая SQLite с инициализированной схемой (не боевой файл проекта)."""
    path = tmp_path / "test_reviews.db"
    init_database(str(path))
    return str(path)


class FakeMessage:
    """Минимальная замена aiogram ``Message``: только то, что читают хендлеры."""

    def __init__(self, text: str, user_id: int = 1, full_name: str = "Тест Пользователь") -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id, full_name=full_name, username=None)
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


@pytest.fixture
def fake_message():
    """Фабрика ``FakeMessage`` (без Telegram)."""
    return FakeMessage


@pytest.fixture
def make_bot_handlers():
    """Фабрика: ``{имя_функции: callback}`` хендлеров бота поверх указанной SQLite."""

    def factory(database_path: str, *, openai_api_key: str | None = "test-key-not-real"):
        settings = Settings(
            telegram_bot_token=None,
            openai_api_key=openai_api_key,
            database_path=database_path,
            openai_model="test-model",
            log_level="INFO",
        )
        router = Router()
        setup_handlers(router, settings)
        return {h.callback.__name__: h.callback for h in router.message.handlers}

    return factory
