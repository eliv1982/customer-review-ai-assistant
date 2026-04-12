"""
Работа с отзывами и SQLite: схема, инициализация, CRUD.

Только стандартный sqlite3, без ORM.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Текст вместо NULL/пустой строки для клиента и товара в ``reviews`` (и backfill старых строк).
REVIEW_DISPLAY_NAME_UNSPECIFIED: str = "Не указано"


def _utc_now_iso() -> str:
    """Текущий момент в UTC в формате ISO 8601 (удобно сортировать и читать в логах)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect(database_path: str) -> sqlite3.Connection:
    """
    Открывает соединение с нужными настройками.

    Каталог для файла БД создаётся при отсутствии — SQLite сам не создаёт родителей.
    """
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Без этого FOREIGN KEY в SQLite не проверяются.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
    """
    Создаёт таблицы, если их ещё нет.

    SQL держим явным списком, чтобы схему было легко ревьюить в диффах.
    """
    statements: Sequence[str] = (
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            customer_name TEXT,
            product_name TEXT,
            rating INTEGER,
            review_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_reviews_created_at
        ON reviews (created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_reviews_status
        ON reviews (status)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_reviews_source_text_created
        ON reviews (source, review_text, created_at)
        """,
        """
        CREATE TABLE IF NOT EXISTS review_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            sentiment TEXT NOT NULL,
            topic TEXT NOT NULL,
            summary TEXT NOT NULL,
            reply_draft TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            UNIQUE (review_id),
            FOREIGN KEY (review_id) REFERENCES reviews (id) ON DELETE CASCADE
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_review_analysis_review_id
        ON review_analysis (review_id)
        """,
    )
    for sql in statements:
        conn.execute(sql)

    # Просмотр в DBeaver: в колонке ``rating`` нет NULL — текст «Не указано», если в таблице reviews.rating NULL.
    # Аналитика AVG(rating) по-прежнему идёт по таблице ``reviews``, не по представлению.
    conn.execute("DROP VIEW IF EXISTS review_full_view")
    conn.execute(
        """
        CREATE VIEW review_full_view AS
        SELECT
            r.id AS review_id,
            r.source,
            r.customer_name,
            r.product_name,
            COALESCE(CAST(r.rating AS TEXT), 'Не указано') AS rating,
            r.review_text,
            r.status,
            r.created_at,
            a.id AS analysis_id,
            a.sentiment,
            a.topic,
            a.summary,
            a.reply_draft,
            a.processed_at
        FROM reviews AS r
        LEFT JOIN review_analysis AS a ON a.review_id = r.id
        """
    )
    logger.info("Таблицы reviews, review_analysis и представление review_full_view проверены/созданы.")


def backfill_review_optional_text_fields(conn: sqlite3.Connection) -> None:
    """
    Заполняет ``product_name`` / ``customer_name`` для уже существующих строк,
    где было NULL или пустая строка. Безопасно вызывать многократно.
    """
    conn.execute(
        """
        UPDATE reviews
        SET product_name = ?
        WHERE product_name IS NULL OR TRIM(product_name) = ''
        """,
        (REVIEW_DISPLAY_NAME_UNSPECIFIED,),
    )
    conn.execute(
        """
        UPDATE reviews
        SET customer_name = ?
        WHERE customer_name IS NULL OR TRIM(customer_name) = ''
        """,
        (REVIEW_DISPLAY_NAME_UNSPECIFIED,),
    )


def init_database(database_path: str) -> None:
    """
    Инициализация файла БД: создать каталог, таблицы и индексы.

    Безопасно вызывать многократно (IF NOT EXISTS).
    """
    try:
        with closing(_connect(database_path)) as conn:
            create_tables(conn)
            backfill_review_optional_text_fields(conn)
            conn.commit()
        logger.info("База данных инициализирована: %s", database_path)
    except sqlite3.Error:
        logger.exception("Ошибка SQLite при инициализации БД: %s", database_path)
        raise


def add_review(
    database_path: str,
    *,
    source: str,
    review_text: str,
    customer_name: str | None = None,
    product_name: str | None = None,
    rating: int | None = None,
    status: str = "new",
) -> int:
    """
    Добавляет новый отзыв. Возвращает id строки.

    ``status`` по умолчанию ``new`` — до появления анализа; дальше его может обновить
    внешний слой (например, на ``analyzed``).

    ``customer_name`` и ``product_name``: при ``None`` или пустой строке в БД пишется
    ``REVIEW_DISPLAY_NAME_UNSPECIFIED`` (не NULL). ``rating`` без значения — NULL.
    """
    created_at = _utc_now_iso()
    cust = (
        REVIEW_DISPLAY_NAME_UNSPECIFIED
        if customer_name is None or not str(customer_name).strip()
        else str(customer_name).strip()
    )
    prod = (
        REVIEW_DISPLAY_NAME_UNSPECIFIED
        if product_name is None or not str(product_name).strip()
        else str(product_name).strip()
    )
    sql = """
        INSERT INTO reviews (
            source, customer_name, product_name, rating, review_text, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    params = (source, cust, prod, rating, review_text, status, created_at)
    try:
        with closing(_connect(database_path)) as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            new_id = int(cur.lastrowid)
        logger.info("Добавлен отзыв id=%s, source=%s", new_id, source)
        return new_id
    except sqlite3.Error:
        logger.exception("Не удалось добавить отзыв (source=%s)", source)
        raise


def get_review_by_id(database_path: str, review_id: int) -> dict[str, Any] | None:
    """Возвращает один отзыв по id или None."""
    sql = "SELECT * FROM reviews WHERE id = ?"
    try:
        with closing(_connect(database_path)) as conn:
            row = conn.execute(sql, (review_id,)).fetchone()
        if row is None:
            return None
        return dict(row)
    except sqlite3.Error:
        logger.exception("Ошибка при чтении отзыва id=%s", review_id)
        raise


def find_recent_duplicate_review(
    database_path: str,
    *,
    source: str,
    review_text: str,
    hours: int = 24,
) -> int | None:
    """
    Ищет ``id`` отзыва с тем же ``source`` и ``review_text``, созданного не раньше
    чем ``hours`` часов назад, для которого уже есть строка в ``review_analysis``.

    Нужен для Telegram: не создавать повторную запись и не вызывать OpenAI при
    повторной отправке того же текста тем же пользователем.
    """
    if hours < 1:
        raise ValueError("hours должен быть >= 1")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    sql = """
        SELECT r.id AS id
        FROM reviews AS r
        INNER JOIN review_analysis AS a ON a.review_id = r.id
        WHERE r.source = ?
          AND r.review_text = ?
          AND r.created_at >= ?
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT 1
    """
    try:
        with closing(_connect(database_path)) as conn:
            row = conn.execute(sql, (source, review_text, cutoff)).fetchone()
        if row is None:
            return None
        return int(row["id"])
    except sqlite3.Error:
        logger.exception(
            "Ошибка при поиске недавнего дубликата (source=%s, len(text)=%s)",
            source,
            len(review_text),
        )
        raise


def find_review_by_source_and_text(
    database_path: str,
    *,
    source: str,
    review_text: str,
) -> tuple[int, str] | None:
    """
    Возвращает (id, status) самой ранней записи с тем же ``source`` и ``review_text``,
    либо ``None``. Нужен для повторного импорта одного и того же CSV без второй вставки.
    """
    sql = """
        SELECT id, status FROM reviews
        WHERE source = ? AND review_text = ?
        ORDER BY id ASC
        LIMIT 1
    """
    try:
        with closing(_connect(database_path)) as conn:
            row = conn.execute(sql, (source, review_text)).fetchone()
        if row is None:
            return None
        return int(row["id"]), str(row["status"])
    except sqlite3.Error:
        logger.exception(
            "Ошибка при поиске дубликата по source=%s и длине текста=%s",
            source,
            len(review_text),
        )
        raise


def update_review_status(database_path: str, review_id: int, status: str) -> None:
    """
    Обновляет поле ``status`` у отзыва (например ``new`` → ``processed`` или ``error``).
    """
    sql = "UPDATE reviews SET status = ? WHERE id = ?"
    try:
        with closing(_connect(database_path)) as conn:
            cur = conn.execute(sql, (status, review_id))
            if cur.rowcount == 0:
                logger.warning("update_review_status: отзыв с id=%s не найден", review_id)
            conn.commit()
        logger.info("Статус отзыва id=%s установлен: %s", review_id, status)
    except sqlite3.Error:
        logger.exception("Ошибка при обновлении статуса отзыва id=%s", review_id)
        raise


def list_reviews(
    database_path: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    Список отзывов с пагинацией, от новых к старым.

    ``limit`` ограничивает размер выборки, чтобы случайно не прочитать весь архив в память.
    """
    if limit < 1:
        raise ValueError("limit должен быть >= 1")
    if offset < 0:
        raise ValueError("offset не может быть отрицательным")
    sql = """
        SELECT * FROM reviews
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
    """
    try:
        with closing(_connect(database_path)) as conn:
            rows = conn.execute(sql, (limit, offset)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        logger.exception("Ошибка при выборке списка отзывов")
        raise


def save_review_analysis(
    database_path: str,
    *,
    review_id: int,
    sentiment: str,
    topic: str,
    summary: str,
    reply_draft: str,
) -> int:
    """
    Сохраняет результат анализа для отзыва.

    Один отзыв — одна строка анализа: при повторном вызове поля обновляются (UPSERT).
    """
    processed_at = _utc_now_iso()
    sql = """
        INSERT INTO review_analysis (
            review_id, sentiment, topic, summary, reply_draft, processed_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(review_id) DO UPDATE SET
            sentiment = excluded.sentiment,
            topic = excluded.topic,
            summary = excluded.summary,
            reply_draft = excluded.reply_draft,
            processed_at = excluded.processed_at
    """
    params = (review_id, sentiment, topic, summary, reply_draft, processed_at)
    try:
        with closing(_connect(database_path)) as conn:
            conn.execute(sql, params)
            row = conn.execute(
                "SELECT id FROM review_analysis WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("Не найдена строка анализа после UPSERT")
            analysis_id = int(row["id"])
            conn.commit()
        logger.info("Сохранён анализ id=%s для отзыва review_id=%s", analysis_id, review_id)
        return analysis_id
    except sqlite3.IntegrityError:
        logger.exception(
            "Нарушение целостности при сохранении анализа (возможно, нет отзыва review_id=%s)",
            review_id,
        )
        raise
    except sqlite3.Error:
        logger.exception("Ошибка при сохранении анализа для review_id=%s", review_id)
        raise


def get_review_with_analysis(
    database_path: str,
    review_id: int,
) -> dict[str, Any] | None:
    """
    Один отзыв и связанный анализ (если есть) в одном словаре.

    Поля анализа префикс ``analysis_``, чтобы не пересекаться с колонками reviews при
    одинаковых именах в будущем; сейчас пересечений нет, префикс всё равно делает JOIN-результат явным.
    """
    sql = """
        SELECT
            r.id AS review_id,
            r.source,
            r.customer_name,
            r.product_name,
            r.rating,
            r.review_text,
            r.status,
            r.created_at,
            a.id AS analysis_id,
            a.sentiment AS analysis_sentiment,
            a.topic AS analysis_topic,
            a.summary AS analysis_summary,
            a.reply_draft AS analysis_reply_draft,
            a.processed_at AS analysis_processed_at
        FROM reviews r
        LEFT JOIN review_analysis a ON a.review_id = r.id
        WHERE r.id = ?
    """
    try:
        with closing(_connect(database_path)) as conn:
            row = conn.execute(sql, (review_id,)).fetchone()
        if row is None:
            return None
        return dict(row)
    except sqlite3.Error:
        logger.exception("Ошибка при JOIN-выборке отзыва id=%s", review_id)
        raise
