"""
Справочный слой: типовые формулировки отзывов, шаблоны ответов и рекомендации из CSV.

Не вызывает OpenAI и не пишет в SQLite — только чтение и подбор записей.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Sequence

from services.ai_service import StructuredReviewAnalysis

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "review_type",
    "common_phrase",
    "sentiment",
    "topic",
    "reply_template",
    "recommended_action",
    "summary_example",
)


@dataclass(frozen=True)
class KnowledgeBaseRow:
    """Одна строка справочника (совпадает по смыслу с колонками CSV)."""

    review_type: str
    common_phrase: str
    sentiment: str
    topic: str
    reply_template: str
    recommended_action: str
    summary_example: str


class KnowledgeBaseError(Exception):
    """Ошибка формата или отсутствия файла базы знаний."""


def _validate_header(fieldnames: Sequence[str] | None) -> None:
    if not fieldnames:
        raise KnowledgeBaseError("В CSV нет строки заголовка.")
    names = {fn.strip() for fn in fieldnames if fn}
    missing = [c for c in REQUIRED_COLUMNS if c not in names]
    if missing:
        raise KnowledgeBaseError(f"В заголовке базы знаний не хватает колонок: {missing}")


def load_knowledge_base(csv_path: str | Path) -> tuple[KnowledgeBaseRow, ...]:
    """
    Загружает весь справочник в память (файл небольшой; pandas не используется).
    """
    path = Path(csv_path)
    if not path.is_file():
        raise KnowledgeBaseError(f"Файл базы знаний не найден: {path}")

    rows: list[KnowledgeBaseRow] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        _validate_header(reader.fieldnames)
        for raw in reader:
            if not any((raw.get(c) or "").strip() for c in REQUIRED_COLUMNS):
                continue
            row = KnowledgeBaseRow(
                review_type=(raw.get("review_type") or "").strip(),
                common_phrase=(raw.get("common_phrase") or "").strip(),
                sentiment=(raw.get("sentiment") or "").strip(),
                topic=(raw.get("topic") or "").strip(),
                reply_template=(raw.get("reply_template") or "").strip(),
                recommended_action=(raw.get("recommended_action") or "").strip(),
                summary_example=(raw.get("summary_example") or "").strip(),
            )
            if not row.topic:
                logger.warning("Пропуск строки KB без topic: %s", row.common_phrase[:40])
                continue
            rows.append(row)

    logger.info("Загружена база знаний: %s записей из %s", len(rows), path)
    return tuple(rows)


def _sentiment_to_review_type(sentiment: str) -> str | None:
    """Грубое сопоставление для ранжирования (не для замены полей модели)."""
    m = {
        "negative": "complaint",
        "positive": "gratitude",
        "neutral": "suggestion",
        "mixed": "mixed",
    }
    return m.get(sentiment.strip())


def _polar_clash(analysis_sentiment: str, row_sentiment: str) -> bool:
    """Несовместимые полюса: благодарность не подбираем к явной негативной оценке и наоборот."""
    a = analysis_sentiment.strip()
    b = row_sentiment.strip()
    if a == "negative" and b == "positive":
        return True
    if a == "positive" and b == "negative":
        return True
    return False


def _review_type_bonus(row: KnowledgeBaseRow, hint_rt: str | None) -> int:
    return 1 if hint_rt and row.review_type == hint_rt else 0


def _tier2_sentiment_rank(analysis_sentiment: str, row_sentiment: str) -> int:
    """
    Выше — «ближе» к контексту при доборе по одной теме без совпадения тональности.
    """
    a = analysis_sentiment.strip()
    b = row_sentiment.strip()
    if a == "negative":
        order = {"mixed": 3, "neutral": 2, "negative": 1, "positive": 0}
    elif a == "positive":
        order = {"mixed": 3, "neutral": 2, "positive": 1, "negative": 0}
    elif a == "neutral":
        order = {"neutral": 3, "mixed": 2, "negative": 1, "positive": 1}
    else:  # mixed
        order = {"mixed": 4, "neutral": 3, "negative": 2, "positive": 2}
    return order.get(b, 0)


def _row_key(r: KnowledgeBaseRow) -> tuple[str, str, str]:
    return (r.topic, r.common_phrase, r.sentiment)


def find_matches_for_analysis(
    rows: Sequence[KnowledgeBaseRow],
    analysis: StructuredReviewAnalysis,
    *,
    limit: int = 5,
) -> list[KnowledgeBaseRow]:
    """
    Подбор по результату анализа ИИ.

    Приоритет:
    1. Совпадение ``topic`` + ``sentiment`` (ранжирование по ``review_type``).
    2. Если не хватает до ``limit`` — добор по той же ``topic``, без полярного конфликта
       с тональностью анализа (не смешиваем negative с positive-шаблонами и наоборот).

    Черновик ``reply_draft`` модели не изменяется.
    """
    if limit < 1:
        raise ValueError("limit должен быть >= 1")

    topic = analysis.topic
    sent = analysis.sentiment
    hint_rt = _sentiment_to_review_type(sent)

    seen: set[tuple[str, str, str]] = set()
    out: list[KnowledgeBaseRow] = []

    tier1 = [r for r in rows if r.topic == topic and r.sentiment == sent]
    tier1.sort(
        key=lambda r: (-_review_type_bonus(r, hint_rt), r.review_type, r.common_phrase),
    )
    for r in tier1:
        if len(out) >= limit:
            break
        k = _row_key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)

    if len(out) < limit:
        tier2 = [
            r
            for r in rows
            if r.topic == topic
            and r.sentiment != sent
            and not _polar_clash(sent, r.sentiment)
        ]
        tier2.sort(
            key=lambda r: (
                -_tier2_sentiment_rank(sent, r.sentiment),
                -_review_type_bonus(r, hint_rt),
                r.review_type,
                r.common_phrase,
            ),
        )
        for r in tier2:
            if len(out) >= limit:
                break
            k = _row_key(r)
            if k not in seen:
                seen.add(k)
                out.append(r)

    if not out:
        # Резерв: только тональность по всем темам (без полярного конфликта).
        fallback = [r for r in rows if r.sentiment == sent and not _polar_clash(sent, r.sentiment)]
        fallback.sort(key=lambda r: (r.topic != "other", r.review_type, r.common_phrase))
        for r in fallback:
            if len(out) >= limit:
                break
            k = _row_key(r)
            if k not in seen:
                seen.add(k)
                out.append(r)

    return out
