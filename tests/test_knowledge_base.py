"""Загрузка базы знаний и подбор по topic/sentiment."""

from __future__ import annotations

from pathlib import Path

from services.ai_service import StructuredReviewAnalysis
from services.knowledge_base_service import find_matches_for_analysis, load_knowledge_base


def _kb_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "knowledge_base.csv"


def test_knowledge_base_loads_and_matches_delivery_negative() -> None:
    rows = load_knowledge_base(_kb_path())
    assert len(rows) >= 1

    analysis = StructuredReviewAnalysis(
        sentiment="negative",
        topic="delivery",
        summary="Доставка задержалась.",
        reply_draft="(тест)",
    )
    matches = find_matches_for_analysis(rows, analysis, limit=3)
    assert len(matches) >= 1
    # Сначала tier1: topic + sentiment как у анализа; далее может быть добор по теме.
    assert matches[0].topic == "delivery"
    assert matches[0].sentiment == "negative"
    assert all(r.topic == "delivery" for r in matches)
