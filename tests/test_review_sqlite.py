"""Вставка и чтение отзыва в SQLite."""

from __future__ import annotations

from services.review_service import REVIEW_DISPLAY_NAME_UNSPECIFIED, add_review, get_review_by_id


def test_add_review_and_read_back(tmp_db: str) -> None:
    rid = add_review(
        tmp_db,
        source="pytest:insert",
        review_text="Тестовый текст отзыва для проверки полей.",
        customer_name="  Клиент Тест  ",
        product_name="Товар X",
        rating=4,
        status="new",
    )
    assert rid > 0

    row = get_review_by_id(tmp_db, rid)
    assert row is not None
    assert row["id"] == rid
    assert row["source"] == "pytest:insert"
    assert row["review_text"] == "Тестовый текст отзыва для проверки полей."
    assert row["customer_name"] == "Клиент Тест"
    assert row["product_name"] == "Товар X"
    assert row["rating"] == 4
    assert row["status"] == "new"
    assert row["created_at"]


def test_add_review_optional_names_use_placeholder(tmp_db: str) -> None:
    rid = add_review(
        tmp_db,
        source="pytest:minimal",
        review_text="Без имён",
        customer_name=None,
        product_name="  ",
        rating=None,
    )
    row = get_review_by_id(tmp_db, rid)
    assert row is not None
    assert row["customer_name"] == REVIEW_DISPLAY_NAME_UNSPECIFIED
    assert row["product_name"] == REVIEW_DISPLAY_NAME_UNSPECIFIED
    assert row["rating"] is None
