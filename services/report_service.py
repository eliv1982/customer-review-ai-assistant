"""
Аналитика и текстовые отчёты по отзывам в SQLite.

Только sqlite3 и агрегаты в SQL; OpenAI и pandas не используются.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.localization_service import (
    rating_display_ru,
    sentiment_label_ru,
    status_label_ru,
    topic_label_ru,
)
from services.review_service import REVIEW_DISPLAY_NAME_UNSPECIFIED

# Источники с префиксом не попадают в пользовательский отчёт Telegram (/report).
TELEGRAM_REPORT_EXCLUDE_SOURCE_PREFIXES: Final[tuple[str, ...]] = ("smoke_",)


def _connect(database_path: str) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _source_filter_sql(table_alias: str, exclude_prefixes: tuple[str, ...]) -> tuple[str, list[str]]:
    """Фрагмент ``AND (...)`` для фильтрации ``source`` (префиксы, регистрозависимо как в SQL LIKE)."""
    if not exclude_prefixes:
        return "", []
    col = f"{table_alias}.source"
    parts = [f"COALESCE({col}, '') NOT LIKE ?" for _ in exclude_prefixes]
    return " AND (" + " AND ".join(parts) + ")", [f"{p}%" for p in exclude_prefixes]


def count_total_reviews(
    database_path: str,
    *,
    exclude_source_prefixes: tuple[str, ...] = (),
) -> int:
    """Общее количество строк в ``reviews`` (с опциональным исключением источников)."""
    frag, params = _source_filter_sql("reviews", exclude_source_prefixes)
    sql = f"SELECT COUNT(*) AS n FROM reviews WHERE 1=1{frag}"
    with closing(_connect(database_path)) as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def count_by_status(
    database_path: str,
    *,
    exclude_source_prefixes: tuple[str, ...] = (),
) -> dict[str, int]:
    """Количество отзывов по полю ``status``."""
    frag, params = _source_filter_sql("reviews", exclude_source_prefixes)
    sql = f"""
        SELECT status, COUNT(*) AS n
        FROM reviews
        WHERE 1=1{frag}
        GROUP BY status
        ORDER BY n DESC, status
    """
    with closing(_connect(database_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {str(r["status"]): int(r["n"]) for r in rows}


def count_by_sentiment(
    database_path: str,
    *,
    exclude_source_prefixes: tuple[str, ...] = (),
) -> dict[str, int]:
    """Количество по ``review_analysis.sentiment`` (только отзывы с анализом)."""
    frag, params = _source_filter_sql("r", exclude_source_prefixes)
    sql = f"""
        SELECT a.sentiment, COUNT(*) AS n
        FROM review_analysis AS a
        INNER JOIN reviews AS r ON r.id = a.review_id
        WHERE 1=1{frag}
        GROUP BY a.sentiment
        ORDER BY n DESC, a.sentiment
    """
    with closing(_connect(database_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {str(r["sentiment"]): int(r["n"]) for r in rows}


def count_by_topic(
    database_path: str,
    *,
    exclude_source_prefixes: tuple[str, ...] = (),
) -> dict[str, int]:
    """Количество по ``review_analysis.topic`` (с учётом фильтра по источнику отзыва)."""
    frag, params = _source_filter_sql("r", exclude_source_prefixes)
    sql = f"""
        SELECT a.topic, COUNT(*) AS n
        FROM review_analysis AS a
        INNER JOIN reviews AS r ON r.id = a.review_id
        WHERE 1=1{frag}
        GROUP BY a.topic
        ORDER BY n DESC, a.topic
    """
    with closing(_connect(database_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {str(r["topic"]): int(r["n"]) for r in rows}


def average_rating(
    database_path: str,
    *,
    exclude_source_prefixes: tuple[str, ...] = (),
) -> float | None:
    """Средний ``rating`` по отзывам с заполненной оценкой; ``None``, если таких нет."""
    frag, params = _source_filter_sql("reviews", exclude_source_prefixes)
    sql = f"""
        SELECT AVG(rating) AS avg_r
        FROM reviews
        WHERE rating IS NOT NULL{frag}
    """
    with closing(_connect(database_path)) as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None or row["avg_r"] is None:
        return None
    return float(row["avg_r"])


def top_problem_topics(
    database_path: str,
    *,
    limit: int = 5,
    exclude_source_prefixes: tuple[str, ...] = (),
) -> list[tuple[str, int]]:
    """
    Темы с негативной или смешанной тональностью в анализе — по убыванию числа случаев.
    """
    if limit < 1:
        raise ValueError("limit должен быть >= 1")
    frag, params = _source_filter_sql("r", exclude_source_prefixes)
    sql = f"""
        SELECT a.topic, COUNT(*) AS n
        FROM review_analysis AS a
        INNER JOIN reviews AS r ON r.id = a.review_id
        WHERE a.sentiment IN ('negative', 'mixed'){frag}
        GROUP BY a.topic
        ORDER BY n DESC, a.topic
        LIMIT ?
    """
    qparams = list(params) + [limit]
    with closing(_connect(database_path)) as conn:
        rows = conn.execute(sql, qparams).fetchall()
    return [(str(r["topic"]), int(r["n"])) for r in rows]


@dataclass(frozen=True)
class ProductSummaryRow:
    """Одна строка сводки по продукту (название из ``reviews.product_name``)."""

    product_label: str
    review_count: int
    avg_rating: float | None


def summary_by_product(
    database_path: str,
    *,
    limit: int = 20,
    exclude_source_prefixes: tuple[str, ...] = (),
    omit_unnamed_products: bool = False,
) -> list[ProductSummaryRow]:
    """
    Сводка по продуктам: число отзывов и средний рейтинг (если оценки есть).
    Пустое имя продукта в агрегате отображается как «Не указано», если не задано ``omit_unnamed_products``.
    При ``omit_unnamed_products`` из сводки исключаются пустые названия и строка «Не указано».
    """
    if limit < 1:
        raise ValueError("limit должен быть >= 1")
    frag, params = _source_filter_sql("reviews", exclude_source_prefixes)
    unspec_sql = REVIEW_DISPLAY_NAME_UNSPECIFIED.replace("'", "''")
    if omit_unnamed_products:
        name_expr = "TRIM(COALESCE(product_name, '')) AS product_label"
        group_expr = "TRIM(COALESCE(product_name, ''))"
        extra = (
            " AND NULLIF(TRIM(COALESCE(product_name, '')), '') IS NOT NULL"
            " AND TRIM(COALESCE(product_name, '')) != ?"
        )
    else:
        name_expr = (
            f"COALESCE(NULLIF(TRIM(product_name), ''), '{unspec_sql}') AS product_label"
        )
        group_expr = f"COALESCE(NULLIF(TRIM(product_name), ''), '{unspec_sql}')"
        extra = ""
    sql = f"""
        SELECT
            {name_expr},
            COUNT(*) AS cnt,
            AVG(rating) AS avg_rating
        FROM reviews
        WHERE 1=1{frag}{extra}
        GROUP BY {group_expr}
        ORDER BY cnt DESC, product_label
        LIMIT ?
    """
    qparams = list(params)
    if omit_unnamed_products:
        qparams.append(REVIEW_DISPLAY_NAME_UNSPECIFIED)
    qparams.append(limit)
    with closing(_connect(database_path)) as conn:
        rows = conn.execute(sql, qparams).fetchall()
    out: list[ProductSummaryRow] = []
    for r in rows:
        ar = r["avg_rating"]
        out.append(
            ProductSummaryRow(
                product_label=str(r["product_label"]),
                review_count=int(r["cnt"]),
                avg_rating=float(ar) if ar is not None else None,
            )
        )
    return out


@dataclass(frozen=True)
class ReviewAnalyticsSnapshot:
    """Снимок показателей для отчёта и API слоёв."""

    total_reviews: int
    by_status: tuple[tuple[str, int], ...]
    by_sentiment: tuple[tuple[str, int], ...]
    by_topic: tuple[tuple[str, int], ...]
    avg_rating: float | None
    top_problem_topics: tuple[tuple[str, int], ...]
    by_product: tuple[ProductSummaryRow, ...]


def build_analytics_snapshot(
    database_path: str,
    *,
    product_limit: int = 20,
    problem_topics_limit: int = 5,
    exclude_source_prefixes: tuple[str, ...] = (),
    omit_unnamed_products: bool = False,
) -> ReviewAnalyticsSnapshot:
    """Собирает все основные агрегаты одним набором запросов к БД."""
    return ReviewAnalyticsSnapshot(
        total_reviews=count_total_reviews(
            database_path, exclude_source_prefixes=exclude_source_prefixes
        ),
        by_status=tuple(
            count_by_status(database_path, exclude_source_prefixes=exclude_source_prefixes).items()
        ),
        by_sentiment=tuple(
            count_by_sentiment(
                database_path, exclude_source_prefixes=exclude_source_prefixes
            ).items()
        ),
        by_topic=tuple(
            count_by_topic(database_path, exclude_source_prefixes=exclude_source_prefixes).items()
        ),
        avg_rating=average_rating(database_path, exclude_source_prefixes=exclude_source_prefixes),
        top_problem_topics=tuple(
            top_problem_topics(
                database_path,
                limit=problem_topics_limit,
                exclude_source_prefixes=exclude_source_prefixes,
            )
        ),
        by_product=tuple(
            summary_by_product(
                database_path,
                limit=product_limit,
                exclude_source_prefixes=exclude_source_prefixes,
                omit_unnamed_products=omit_unnamed_products,
            )
        ),
    )


def format_report_ru(snapshot: ReviewAnalyticsSnapshot, *, compact: bool = False) -> str:
    """Краткий текстовый отчёт на русском по уже посчитанным агрегатам."""
    gap = "\n" if compact else "\n\n"
    lines: list[str] = ["Сводка по отзывам", f"Всего: {snapshot.total_reviews}"]

    lines.append(f"{gap}{'По этапам:' if compact else 'По этапам обработки:'}")
    if snapshot.by_status:
        for status, n in snapshot.by_status:
            lines.append(f"• {status_label_ru(status)}: {n}")
    else:
        lines.append("• (нет данных)")

    lines.append(f"{gap}{'Тональность:' if compact else 'По тональности:'}")
    if snapshot.by_sentiment:
        for s, n in snapshot.by_sentiment:
            lines.append(f"• {sentiment_label_ru(s)}: {n}")
    else:
        lines.append("• (пока нет данных по тональности)")

    lines.append(f"{gap}{'Темы:' if compact else 'По темам:'}")
    if snapshot.by_topic:
        for t, n in snapshot.by_topic:
            lines.append(f"• {topic_label_ru(t)}: {n}")
    else:
        lines.append("• (нет данных)")

    if snapshot.avg_rating is not None:
        avg_line = f"Средняя оценка (где указан балл): {snapshot.avg_rating:.2f}"
    else:
        avg_line = f"Средняя оценка: {rating_display_ru(None)} (нет отзывов с баллом)"
    lines.append(f"{gap}{avg_line}")

    prob_title = (
        "Сложные отзывы (темы):"
        if compact
        else "Темы, которые чаще встречаются в сложных отзывах:"
    )
    lines.append(f"{gap}{prob_title}")
    if snapshot.top_problem_topics:
        for t, n in snapshot.top_problem_topics:
            lines.append(f"• {topic_label_ru(t)}: {n}")
    else:
        lines.append("• (пока нет таких отзывов)")

    lines.append(f"{gap}По товарам:")
    if snapshot.by_product:
        for p in snapshot.by_product:
            ar = f"{p.avg_rating:.2f}" if p.avg_rating is not None else rating_display_ru(None)
            lines.append(f"• {p.product_label}: {p.review_count} отз., ср. {ar}")
    else:
        lines.append(
            "• (нет строк с названием товара)" if compact else "• (нет отзывов)"
        )

    return "\n".join(lines)
