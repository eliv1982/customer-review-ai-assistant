"""
Пайплайн: сохранение отзыва → анализ OpenAI → запись анализа и статуса в SQLite.

Оркестрация поверх ``review_service`` и ``ai_service`` без изменения их публичного API.
После успешного анализа дополнительно подбираются строки справочника (``kb_matches``).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from services.ai_service import (
    AnalysisError,
    SENTIMENT_VALUES,
    TOPIC_VALUES,
    Sentiment,
    StructuredReviewAnalysis,
    Topic,
    analyze_review,
)
from services.knowledge_base_service import (
    KnowledgeBaseRow,
    find_matches_for_analysis,
    load_knowledge_base,
)
from services.review_service import (
    add_review,
    get_review_by_id,
    get_review_with_analysis,
    save_review_analysis,
    update_review_status,
)

logger = logging.getLogger(__name__)

_DEFAULT_KB_CSV = Path(__file__).resolve().parent.parent / "data" / "knowledge_base.csv"


def _kb_matches_for_analysis(
    analysis: StructuredReviewAnalysis,
    knowledge_base_path: str | Path | None,
) -> tuple[KnowledgeBaseRow, ...]:
    """Справочные совпадения по полям анализа ИИ; не влияет на ``reply_draft`` модели."""
    path = Path(knowledge_base_path) if knowledge_base_path is not None else _DEFAULT_KB_CSV
    if not path.is_file():
        logger.debug("База знаний не найдена (%s), kb_matches пустой", path)
        return ()
    try:
        rows = load_knowledge_base(path)
    except Exception:
        logger.exception("Не удалось загрузить базу знаний из %s", path)
        return ()
    return tuple(find_matches_for_analysis(rows, analysis, limit=3))


@dataclass(frozen=True)
class ProcessedReviewResult:
    """
    Итог обработки одного отзыва через БД и OpenAI.

    - ``status``: ``processed`` — анализ сохранён; ``error`` — сбой анализа или записи анализа.
    - ``review``: строка из ``reviews`` (как после ``get_review_by_id``).
    - ``analysis``: структурированный ответ модели; при ``error`` обычно ``None``.
    - ``kb_matches``: подсказки из CSV базы знаний (пусто, если анализа нет или файл недоступен).
    """

    review_id: int
    status: Literal["processed", "error"]
    review: dict[str, Any]
    analysis: StructuredReviewAnalysis | None
    kb_matches: tuple[KnowledgeBaseRow, ...] = ()


def _row_or_raise(database_path: str, review_id: int) -> dict[str, Any]:
    row = get_review_by_id(database_path, review_id)
    if row is None:
        raise sqlite3.DatabaseError(f"Отзыв id={review_id} не найден")
    return row


def _analysis_from_stored_join(row: dict[str, Any]) -> StructuredReviewAnalysis | None:
    """Восстанавливает структуру анализа из строки ``get_review_with_analysis``."""
    if row.get("analysis_id") is None:
        return None
    sent = str(row.get("analysis_sentiment") or "").strip()
    topic = str(row.get("analysis_topic") or "").strip()
    summary = str(row.get("analysis_summary") or "").strip()
    reply_draft = str(row.get("analysis_reply_draft") or "").strip()
    if sent not in SENTIMENT_VALUES or topic not in TOPIC_VALUES:
        logger.warning(
            "Пропуск восстановления анализа из БД: неизвестные sentiment/topic (review_id=%s)",
            row.get("review_id"),
        )
        return None
    if not summary or not reply_draft:
        return None
    return StructuredReviewAnalysis(
        sentiment=cast(Sentiment, sent),
        topic=cast(Topic, topic),
        summary=summary,
        reply_draft=reply_draft,
    )


def load_processed_result_from_database(
    database_path: str,
    review_id: int,
    *,
    knowledge_base_path: str | Path | None = None,
) -> ProcessedReviewResult | None:
    """
    Собирает ``ProcessedReviewResult`` из ``reviews`` + ``review_analysis`` и KB,
    без вызова OpenAI. ``None``, если отзыва нет или анализ ещё не сохранён.
    """
    joined = get_review_with_analysis(database_path, review_id)
    if joined is None:
        return None
    analysis = _analysis_from_stored_join(joined)
    if analysis is None:
        return None
    review = get_review_by_id(database_path, review_id)
    if review is None:
        return None
    kb_matches = _kb_matches_for_analysis(analysis, knowledge_base_path)
    st: Literal["processed", "error"] = (
        "processed" if str(review.get("status") or "") == "processed" else "error"
    )
    return ProcessedReviewResult(
        review_id=review_id,
        status=st,
        review=review,
        analysis=analysis,
        kb_matches=kb_matches,
    )


def _run_openai_pipeline_for_review(
    database_path: str,
    review_id: int,
    review_text: str,
    *,
    api_key: str | None,
    model: str,
    knowledge_base_path: str | Path | None = None,
) -> ProcessedReviewResult:
    """
    Анализ OpenAI + сохранение ``review_analysis`` + статус ``processed`` / ``error``.

    Строка в ``reviews`` уже должна существовать (``review_id``).
    """
    text = (review_text or "").strip()
    if not text:
        logger.warning("Пайплайн: пустой review_text для review_id=%s", review_id)
        update_review_status(database_path, review_id, "error")
        return ProcessedReviewResult(
            review_id=review_id,
            status="error",
            review=_row_or_raise(database_path, review_id),
            analysis=None,
            kb_matches=(),
        )

    try:
        analysis = analyze_review(
            api_key=api_key,
            model=model,
            review_text=text,
        )
        logger.info("Пайплайн: OpenAI вернул анализ для review_id=%s", review_id)
    except AnalysisError as exc:
        logger.warning("Пайплайн: сбой анализа OpenAI для review_id=%s: %s", review_id, exc)
        update_review_status(database_path, review_id, "error")
        return ProcessedReviewResult(
            review_id=review_id,
            status="error",
            review=_row_or_raise(database_path, review_id),
            analysis=None,
            kb_matches=(),
        )
    except Exception:
        logger.exception("Пайплайн: неожиданная ошибка OpenAI для review_id=%s", review_id)
        update_review_status(database_path, review_id, "error")
        return ProcessedReviewResult(
            review_id=review_id,
            status="error",
            review=_row_or_raise(database_path, review_id),
            analysis=None,
            kb_matches=(),
        )

    kb_matches = _kb_matches_for_analysis(analysis, knowledge_base_path)

    try:
        save_review_analysis(
            database_path,
            review_id=review_id,
            sentiment=analysis.sentiment,
            topic=analysis.topic,
            summary=analysis.summary,
            reply_draft=analysis.reply_draft,
        )
    except sqlite3.Error:
        logger.exception("Пайплайн: не удалось сохранить анализ для review_id=%s", review_id)
        update_review_status(database_path, review_id, "error")
        return ProcessedReviewResult(
            review_id=review_id,
            status="error",
            review=_row_or_raise(database_path, review_id),
            analysis=analysis,
            kb_matches=kb_matches,
        )

    update_review_status(database_path, review_id, "processed")
    logger.info("Пайплайн: отзыв id=%s обработан (processed)", review_id)

    return ProcessedReviewResult(
        review_id=review_id,
        status="processed",
        review=_row_or_raise(database_path, review_id),
        analysis=analysis,
        kb_matches=kb_matches,
    )


def process_review(
    database_path: str,
    *,
    api_key: str | None,
    model: str,
    source: str,
    review_text: str,
    customer_name: str | None = None,
    product_name: str | None = None,
    rating: int | None = None,
    knowledge_base_path: str | Path | None = None,
) -> ProcessedReviewResult:
    """
    Полный цикл: INSERT отзыва (``new``) → анализ OpenAI → UPSERT ``review_analysis`` → ``processed``.

    При ошибке анализа или при ошибке записи анализа в БД статус отзыва — ``error``.
    """
    review_id = add_review(
        database_path,
        source=source,
        review_text=review_text,
        customer_name=customer_name,
        product_name=product_name,
        rating=rating,
        status="new",
    )
    logger.info("Пайплайн: отзыв id=%s создан со статусом new", review_id)

    return _run_openai_pipeline_for_review(
        database_path,
        review_id,
        review_text,
        api_key=api_key,
        model=model,
        knowledge_base_path=knowledge_base_path,
    )


def process_existing_review(
    database_path: str,
    review_id: int,
    *,
    api_key: str | None,
    model: str,
    knowledge_base_path: str | Path | None = None,
) -> ProcessedReviewResult:
    """
    Обработка уже существующей строки ``reviews``: чтение ``review_text`` → OpenAI → ``review_analysis`` → статус.

    Новая запись в ``reviews`` **не** создаётся (в отличие от ``process_review``).
    """
    row = get_review_by_id(database_path, review_id)
    if row is None:
        raise ValueError(f"Отзыв с id={review_id} не найден в базе")

    review_text = str(row.get("review_text") or "")
    logger.info(
        "Пайплайн (существующий отзыв): старт обработки review_id=%s, source=%s",
        review_id,
        row.get("source"),
    )

    return _run_openai_pipeline_for_review(
        database_path,
        review_id,
        review_text,
        api_key=api_key,
        model=model,
        knowledge_base_path=knowledge_base_path,
    )
