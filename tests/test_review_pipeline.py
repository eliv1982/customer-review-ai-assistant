"""Пайплайн отзыва: анализ → запись анализа → статус. OpenAI подменён, SQLite настоящая."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from services import review_pipeline
from services.ai_service import AnalysisError, StructuredReviewAnalysis
from services.review_pipeline import (
    load_processed_result_from_database,
    process_existing_review,
    process_review,
)
from services.review_service import add_review, get_review_with_analysis, list_reviews

_ANALYSIS = StructuredReviewAnalysis(
    sentiment="negative",
    topic="delivery",
    summary="Доставка задержалась.",
    reply_draft="Приносим извинения.",
)

_KB_HEADER = (
    "review_type,common_phrase,sentiment,topic,reply_template,recommended_action,summary_example\n"
)


@pytest.fixture
def kb_csv(tmp_path):
    """Маленький справочник: одна подходящая строка и одна для другой темы."""
    path = tmp_path / "kb.csv"
    path.write_text(
        _KB_HEADER
        + "complaint,Заказ ехал долго,negative,delivery,Шаблон про доставку,Проверить SLA,Сводка\n"
        + "complaint,Сломан сайт,negative,website,Шаблон про сайт,Проверить сайт,Сводка\n",
        encoding="utf-8",
    )
    return path


class _Analyzer:
    """Заглушка ``analyze_review``: фиксирует вызовы, возвращает анализ или бросает исключение."""

    def __init__(self, outcome: StructuredReviewAnalysis | Exception = _ANALYSIS) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> StructuredReviewAnalysis:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def analyzer(monkeypatch):
    stub = _Analyzer()
    monkeypatch.setattr(review_pipeline, "analyze_review", stub)
    return stub


def _process(tmp_db, kb_csv, text="Курьер опоздал на два часа.", **kwargs):
    return process_review(
        tmp_db,
        api_key="test-key-not-real",
        model="test-model",
        source="pytest:pipeline",
        review_text=text,
        knowledge_base_path=kb_csv,
        **kwargs,
    )


def test_successful_analysis_is_persisted_and_returned(tmp_db, kb_csv, analyzer) -> None:
    result = _process(tmp_db, kb_csv, customer_name="Анна", product_name="Чайник", rating=2)

    assert result.status == "processed"
    assert result.analysis == _ANALYSIS
    assert result.review["status"] == "processed"
    assert result.review["customer_name"] == "Анна"
    assert result.review["rating"] == 2
    assert [(r.topic, r.sentiment) for r in result.kb_matches[:1]] == [("delivery", "negative")]

    stored = get_review_with_analysis(tmp_db, result.review_id)
    assert stored is not None
    assert stored["status"] == "processed"
    assert (
        stored["analysis_sentiment"],
        stored["analysis_topic"],
        stored["analysis_summary"],
        stored["analysis_reply_draft"],
    ) == ("negative", "delivery", "Доставка задержалась.", "Приносим извинения.")

    (call,) = analyzer.calls
    assert call == {
        "api_key": "test-key-not-real",
        "model": "test-model",
        "review_text": "Курьер опоздал на два часа.",
    }


def test_analysis_error_marks_review_as_error_and_keeps_the_row(tmp_db, kb_csv, analyzer) -> None:
    analyzer.outcome = AnalysisError("модель недоступна")

    result = _process(tmp_db, kb_csv)

    assert result.status == "error"
    assert result.analysis is None
    assert result.kb_matches == ()
    assert result.review["status"] == "error"
    stored = get_review_with_analysis(tmp_db, result.review_id)
    assert stored is not None and stored["analysis_id"] is None
    assert len(list_reviews(tmp_db)) == 1


def test_unexpected_analysis_exception_also_ends_as_error(tmp_db, kb_csv, analyzer) -> None:
    analyzer.outcome = RuntimeError("неожиданный сбой SDK")

    result = _process(tmp_db, kb_csv)

    assert result.status == "error"
    assert result.analysis is None
    assert result.review["status"] == "error"


def test_failed_analysis_write_marks_error_but_returns_the_analysis(
    tmp_db, kb_csv, analyzer
) -> None:
    # Настоящий сбой записи: без таблицы анализа INSERT падает с sqlite3.OperationalError.
    with closing(sqlite3.connect(tmp_db)) as conn:
        conn.execute("DROP TABLE review_analysis")
        conn.commit()

    result = _process(tmp_db, kb_csv)

    assert result.status == "error"
    assert result.analysis == _ANALYSIS  # анализ получен, сохранить не удалось
    assert result.kb_matches
    assert result.review["status"] == "error"
    assert len(analyzer.calls) == 1


def test_blank_review_text_is_error_without_calling_the_model(tmp_db, kb_csv, analyzer) -> None:
    result = _process(tmp_db, kb_csv, text="   \n ")

    assert result.status == "error"
    assert result.analysis is None
    assert analyzer.calls == []


def test_missing_knowledge_base_file_does_not_block_processing(
    tmp_db, tmp_path, analyzer
) -> None:
    result = process_review(
        tmp_db,
        api_key="test-key-not-real",
        model="test-model",
        source="pytest:pipeline",
        review_text="Текст отзыва.",
        knowledge_base_path=tmp_path / "нет_такого_файла.csv",
    )

    assert result.status == "processed"
    assert result.kb_matches == ()


def test_failed_review_is_processed_on_retry_without_a_second_row(tmp_db, kb_csv, analyzer) -> None:
    review_id = add_review(tmp_db, source="pytest:retry", review_text="Повторная попытка.")

    analyzer.outcome = AnalysisError("временный сбой")
    first = process_existing_review(
        tmp_db, review_id, api_key="k", model="m", knowledge_base_path=kb_csv
    )
    analyzer.outcome = _ANALYSIS
    second = process_existing_review(
        tmp_db, review_id, api_key="k", model="m", knowledge_base_path=kb_csv
    )

    assert (first.status, second.status) == ("error", "processed")
    assert second.review_id == review_id
    assert second.analysis == _ANALYSIS
    assert len(list_reviews(tmp_db)) == 1
    stored = get_review_with_analysis(tmp_db, review_id)
    assert stored is not None and stored["status"] == "processed"


def test_process_existing_review_rejects_unknown_id(tmp_db, analyzer) -> None:
    with pytest.raises(ValueError, match="не найден"):
        process_existing_review(tmp_db, 999, api_key="k", model="m")

    assert analyzer.calls == []


# --- восстановление результата из БД (дубликаты) ------------------------------------------------


def test_stored_result_is_restored_without_calling_the_model(tmp_db, kb_csv, analyzer) -> None:
    processed = _process(tmp_db, kb_csv)
    analyzer.outcome = AssertionError("модель не должна вызываться")

    restored = load_processed_result_from_database(
        tmp_db, processed.review_id, knowledge_base_path=kb_csv
    )

    assert restored is not None
    assert restored.status == "processed"
    assert restored.analysis == _ANALYSIS
    assert restored.kb_matches == processed.kb_matches
    assert len(analyzer.calls) == 1


def test_review_without_analysis_is_not_restored(tmp_db) -> None:
    review_id = add_review(tmp_db, source="pytest:restore", review_text="Без анализа.")

    assert load_processed_result_from_database(tmp_db, review_id) is None
    assert load_processed_result_from_database(tmp_db, 999) is None


def test_stored_analysis_with_unknown_enum_is_not_restored(tmp_db, kb_csv, analyzer) -> None:
    processed = _process(tmp_db, kb_csv)
    with closing(sqlite3.connect(tmp_db)) as conn:
        conn.execute("UPDATE review_analysis SET sentiment = 'furious'")
        conn.commit()

    assert load_processed_result_from_database(tmp_db, processed.review_id) is None
