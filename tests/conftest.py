"""Общие фикстуры для pytest."""

from __future__ import annotations

import errno
import socket
from types import SimpleNamespace

import pytest
from aiogram import Router

from bot.handlers import setup_handlers
from config import Settings
from services.review_service import init_database


def _is_local_address(address) -> bool:
    if not isinstance(address, tuple):  # AF_UNIX и т.п.
        return True
    host = str(address[0])
    return host in ("", "localhost", "::1") or host.startswith("127.")


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    """
    Тесты не ходят во внешнюю сеть (OpenAI, Telegram): попытка соединения роняет тест.

    Loopback разрешён — на Windows через него работает ``socket.socketpair`` у asyncio.
    Попытка записывается и проверяется после теста, чтобы её не проглотил ``except Exception``
    в проверяемом коде.
    """
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        if not _is_local_address(address):
            attempts.append(repr(address))
            raise OSError(f"внешняя сеть заблокирована в тестах: {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _is_local_address(address):
            attempts.append(repr(address))
            return errno.ENETUNREACH
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
    assert not attempts, f"тест обратился к внешней сети: {attempts}"


@pytest.fixture
def tmp_db(tmp_path):
    """Пустая SQLite с инициализированной схемой (не боевой файл проекта)."""
    path = tmp_path / "test_reviews.db"
    init_database(str(path))
    return str(path)


class FakeMessage:
    """Минимальная замена aiogram ``Message``: только то, что читают хендлеры."""

    def __init__(
        self, text: str, user_id: int | None = 1, full_name: str = "Тест Пользователь"
    ) -> None:
        self.text = text
        # ``user_id=None`` — сообщение без отправителя (``from_user is None``).
        self.from_user = (
            None
            if user_id is None
            else SimpleNamespace(id=user_id, full_name=full_name, username=None)
        )
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

    def factory(
        database_path: str,
        *,
        openai_api_key: str | None = "test-key-not-real",
        allowed_telegram_ids: frozenset[int] = frozenset(),
    ):
        settings = Settings(
            telegram_bot_token=None,
            openai_api_key=openai_api_key,
            database_path=database_path,
            openai_model="test-model",
            log_level="INFO",
            allowed_telegram_ids=allowed_telegram_ids,
        )
        router = Router()
        setup_handlers(router, settings)
        return {h.callback.__name__: h.callback for h in router.message.handlers}

    return factory
