"""Поиск недавнего дубликата по source + review_text (с анализом)."""

from __future__ import annotations

from services.review_service import (
    add_review,
    find_recent_duplicate_review,
    save_review_analysis,
)


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
