"""Отчёт по отзывам: детерминированные агрегаты, фильтр по источнику, формы слова «отзыв»."""

from __future__ import annotations

import pytest

from services.report_service import (
    TELEGRAM_REPORT_EXCLUDE_SOURCE_PREFIXES,
    ProductSummaryRow,
    _reviews_count_phrase_ru,
    average_rating,
    build_analytics_snapshot,
    count_by_sentiment,
    count_by_status,
    count_by_topic,
    count_total_reviews,
    format_report_ru,
    summary_by_product,
    top_problem_topics,
)
from services.review_service import add_review, save_review_analysis

# (source, product, rating, status, (sentiment, topic) | None)
_SEED = [
    ("telegram:1", "Чайник", 5, "processed", ("positive", "delivery")),
    ("telegram:1", "Чайник", 1, "processed", ("negative", "delivery")),
    ("telegram:2", "Чайник", None, "processed", ("mixed", "support")),
    ("csv_sample", "Фен", 2, "processed", ("negative", "packaging")),
    ("csv_sample", "Фен", 4, "processed", ("neutral", "price")),
    ("telegram:3", None, 3, "error", None),
    ("smoke_kb", "Кофеварка Z (smoke)", 2, "processed", ("negative", "delivery")),
    # Начинается с «smoke», но не с «smoke_»: в отчёте остаётся.
    ("smokehouse", "Коптильня", 5, "processed", ("positive", "other")),
]

_EXCLUDE = TELEGRAM_REPORT_EXCLUDE_SOURCE_PREFIXES


@pytest.fixture
def seeded_db(tmp_db):
    for index, (source, product, rating, status, analysis) in enumerate(_SEED, start=1):
        review_id = add_review(
            tmp_db,
            source=source,
            review_text=f"Отзыв номер {index}",
            product_name=product,
            rating=rating,
            status=status,
        )
        if analysis is not None:
            sentiment, topic = analysis
            save_review_analysis(
                tmp_db,
                review_id=review_id,
                sentiment=sentiment,
                topic=topic,
                summary="Кратко.",
                reply_draft="Ответ.",
            )
    return tmp_db


# --- фильтр по источнику ------------------------------------------------------------------------


def test_telegram_report_excludes_smoke_prefix() -> None:
    assert _EXCLUDE == ("smoke_",)


def test_smoke_source_is_excluded_from_every_aggregate(seeded_db) -> None:
    kwargs = {"exclude_source_prefixes": _EXCLUDE}

    assert count_total_reviews(seeded_db, **kwargs) == 7
    assert count_by_status(seeded_db, **kwargs) == {"processed": 6, "error": 1}
    assert count_by_sentiment(seeded_db, **kwargs) == {
        "negative": 2,
        "positive": 2,
        "mixed": 1,
        "neutral": 1,
    }
    assert count_by_topic(seeded_db, **kwargs) == {
        "delivery": 2,
        "other": 1,
        "packaging": 1,
        "price": 1,
        "support": 1,
    }
    assert average_rating(seeded_db, **kwargs) == pytest.approx(20 / 6)
    assert top_problem_topics(seeded_db, **kwargs) == [
        ("delivery", 1),
        ("packaging", 1),
        ("support", 1),
    ]
    assert "Кофеварка Z (smoke)" not in {p.product_label for p in summary_by_product(seeded_db, **kwargs)}


def test_without_exclusion_smoke_rows_are_counted(seeded_db) -> None:
    assert count_total_reviews(seeded_db) == 8
    assert count_by_status(seeded_db) == {"processed": 7, "error": 1}
    assert count_by_sentiment(seeded_db)["negative"] == 3
    assert average_rating(seeded_db) == pytest.approx(22 / 7)
    assert top_problem_topics(seeded_db)[0] == ("delivery", 2)
    assert "Кофеварка Z (smoke)" in {p.product_label for p in summary_by_product(seeded_db)}


def test_prefix_is_matched_literally_not_as_like_pattern(tmp_db) -> None:
    for source in ("smoke_x", "smokehouse", "smoke%y", "telegram:smoke_1"):
        add_review(tmp_db, source=source, review_text=f"Текст {source}")

    # «_» в префиксе — обычный символ: «smokehouse» не исключается.
    assert count_total_reviews(tmp_db, exclude_source_prefixes=("smoke_",)) == 3
    # «%» тоже: исключается только «smoke%y», а не всё, что начинается с «smoke».
    assert count_total_reviews(tmp_db, exclude_source_prefixes=("smoke%",)) == 3
    # Префикс, а не вхождение: «telegram:smoke_1» остаётся в обоих случаях.
    assert count_total_reviews(tmp_db, exclude_source_prefixes=("smoke_", "smoke%")) == 2


# --- агрегаты -----------------------------------------------------------------------------------


def test_product_summary_counts_and_averages_only_rated_rows(seeded_db) -> None:
    assert summary_by_product(seeded_db, exclude_source_prefixes=_EXCLUDE) == [
        ProductSummaryRow("Чайник", 3, 3.0),  # AVG игнорирует отзыв без оценки: (5 + 1) / 2
        ProductSummaryRow("Фен", 2, 3.0),
        ProductSummaryRow("Коптильня", 1, 5.0),
        ProductSummaryRow("Не указано", 1, 3.0),
    ]


def test_omit_unnamed_products_drops_the_placeholder_row(seeded_db) -> None:
    rows = summary_by_product(seeded_db, exclude_source_prefixes=_EXCLUDE, omit_unnamed_products=True)

    assert [r.product_label for r in rows] == ["Чайник", "Фен", "Коптильня"]


def test_average_rating_is_none_without_any_rating(tmp_db) -> None:
    add_review(tmp_db, source="pytest:none", review_text="Без оценки", rating=None)

    assert average_rating(tmp_db) is None


def test_top_problem_topics_respects_limit(seeded_db) -> None:
    assert top_problem_topics(seeded_db, limit=1) == [("delivery", 2)]


def test_aggregates_are_deterministic(seeded_db) -> None:
    kwargs = {"exclude_source_prefixes": _EXCLUDE, "omit_unnamed_products": True}

    assert build_analytics_snapshot(seeded_db, **kwargs) == build_analytics_snapshot(seeded_db, **kwargs)


# --- текст отчёта -------------------------------------------------------------------------------


def test_telegram_report_text_for_seeded_database(seeded_db) -> None:
    snapshot = build_analytics_snapshot(
        seeded_db,
        exclude_source_prefixes=_EXCLUDE,
        omit_unnamed_products=True,
        product_limit=12,
        problem_topics_limit=4,
    )

    lines = format_report_ru(snapshot, compact=True).splitlines()

    for expected in (
        "Сводка по отзывам",
        "Всего: 7",
        "• обработан: 6",
        "• ошибка: 1",
        "• негативная: 2",
        "• позитивная: 2",
        "• доставка: 2",
        "Средняя оценка (где указан балл): 3.33",
        "• упаковка: 1",
        "• Чайник — 3 отзыва, ср. балл 3.00",
        "• Фен — 2 отзыва, ср. балл 3.00",
        "• Коптильня — 1 отзыв, ср. балл 5.00",
    ):
        assert expected in lines
    assert not any("smoke" in line.lower() for line in lines)


def test_report_for_empty_database(tmp_db) -> None:
    text = format_report_ru(build_analytics_snapshot(tmp_db), compact=True)

    assert "Всего: 0" in text
    assert "(нет данных)" in text
    assert "Средняя оценка: не указана (нет отзывов с баллом)" in text
    assert "Кратко" not in text


@pytest.mark.parametrize(
    ("count", "phrase"),
    [
        (0, "0 отзывов"),
        (1, "1 отзыв"),
        (2, "2 отзыва"),
        (4, "4 отзыва"),
        (5, "5 отзывов"),
        (11, "11 отзывов"),
        (12, "12 отзывов"),
        (14, "14 отзывов"),
        (19, "19 отзывов"),
        (20, "20 отзывов"),
        (21, "21 отзыв"),
        (22, "22 отзыва"),
        (25, "25 отзывов"),
        (101, "101 отзыв"),
        (111, "111 отзывов"),
        (112, "112 отзывов"),
    ],
)
def test_review_count_word_forms(count: int, phrase: str) -> None:
    assert _reviews_count_phrase_ru(count) == phrase
