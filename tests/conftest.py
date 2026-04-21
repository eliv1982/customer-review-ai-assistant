"""Общие фикстуры для pytest."""

from __future__ import annotations

import pytest

from services.review_service import init_database


@pytest.fixture
def tmp_db(tmp_path):
    """Пустая SQLite с инициализированной схемой (не боевой файл проекта)."""
    path = tmp_path / "test_reviews.db"
    init_database(str(path))
    return str(path)
