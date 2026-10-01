"""Поиск недавнего дубликата по source + review_text (с анализом)."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from bot import messages as msg
from services import review_pipeline
from services.ai_service import StructuredReviewAnalysis
from services.review_service import (
    add_review,
    find_recent_duplicate_review,
    list_reviews,
    save_review_analysis,
)

_ANALYSIS = StructuredReviewAnalysis(
    sentiment="negative",
    topic="delivery",
    summary="Доставка задержалась.",
    reply_draft="Приносим извинения.",
)


def _age_review(database_path: str, review_id: int, hours: float) -> None:
    """Сдвигает ``created_at`` отзыва в прошлое (формат как у ``review_service``)."""
    created_at = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0)
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute("UPDATE reviews SET created_at = ? WHERE id = ?", (created_at.isoformat(), review_id))
        conn.commit()


def _add_analysed_review(database_path: str, *, source: str, text: str) -> int:
    review_id = add_review(database_path, source=source, review_text=text, rating=3)
    save_review_analysis(
        database_path,
        review_id=review_id,
        sentiment="negative",
        topic="delivery",
        summary="Кратко.",
        reply_draft="Черновик.",
    )
    return review_id


def test_find_recent_duplicate_returns_same_review_id(tmp_db: str) -> None:
    source = "pytest:dup"
    text = "Один и тот же текст отзыва для дедупликации."

    rid = add_review(tmp_db, source=source, review_text=text, rating=3)
    save_review_analysis(
        tmp_db,
        review_id=rid,
        sentiment="negative",
        topic="delivery",
        summary="Кратко.",
        reply_draft="Черновик.",
    )

    found = find_recent_duplicate_review(
        tmp_db,
        source=source,
        review_text=text,
        hours=24,
    )
    assert found == rid


@pytest.mark.parametrize(
    ("hours_ago", "is_duplicate"),
    [(1, True), (23, True), (25, False), (24 * 7, False)],
)
def test_duplicate_is_found_only_inside_the_24_hour_window(
    tmp_db: str, hours_ago: float, is_duplicate: bool
) -> None:
    rid = _add_analysed_review(tmp_db, source="pytest:window", text="Текст для окна дедупликации.")
    _age_review(tmp_db, rid, hours_ago)

    found = find_recent_duplicate_review(
        tmp_db, source="pytest:window", review_text="Текст для окна дедупликации.", hours=24
    )

    assert found == (rid if is_duplicate else None)


def test_review_without_analysis_is_not_a_duplicate(tmp_db: str) -> None:
    # Неудавшийся анализ не должен «закешировать» отзыв: повторная отправка обрабатывается заново.
    add_review(tmp_db, source="pytest:noanalysis", review_text="Анализ не сохранился.", status="error")

    assert (
        find_recent_duplicate_review(
            tmp_db, source="pytest:noanalysis", review_text="Анализ не сохранился.", hours=24
        )
        is None
    )


def test_duplicate_requires_same_source_and_same_text(tmp_db: str) -> None:
    _add_analysed_review(tmp_db, source="pytest:a", text="Одинаковый текст.")

    assert find_recent_duplicate_review(tmp_db, source="pytest:b", review_text="Одинаковый текст.") is None
    assert find_recent_duplicate_review(tmp_db, source="pytest:a", review_text="Другой текст.") is None


def test_telegram_repeat_is_cached_inside_24_hours_and_reprocessed_after(
    tmp_db, monkeypatch, make_bot_handlers, fake_message
) -> None:
    calls: list[str] = []

    def fake_analyze(**kwargs):
        calls.append(kwargs["review_text"])
        return _ANALYSIS

    monkeypatch.setattr(review_pipeline, "analyze_review", fake_analyze)
    handlers = make_bot_handlers(tmp_db)
    text = "Курьер опоздал на два часа."
    first, repeat, after_window = (fake_message(text, user_id=5) for _ in range(3))

    async def scenario() -> None:
        await handlers["on_review_text"](first)
        await handlers["on_review_text"](repeat)
        # Первый отзыв «состарился» за пределы окна: тот же текст обрабатывается заново.
        _age_review(tmp_db, list_reviews(tmp_db)[0]["id"], hours=25)
        await handlers["on_review_text"](after_window)

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert calls == [text, text], "ровно два вызова модели: первый и после выхода из окна"
    assert not first.answers[0].startswith(msg.DUPLICATE_TELEGRAM_REVIEW_CACHED)
    assert repeat.answers[0].startswith(msg.DUPLICATE_TELEGRAM_REVIEW_CACHED)
    assert not after_window.answers[0].startswith(msg.DUPLICATE_TELEGRAM_REVIEW_CACHED)
    assert len(list_reviews(tmp_db)) == 2
