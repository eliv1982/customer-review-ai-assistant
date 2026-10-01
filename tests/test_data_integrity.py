"""Целостность данных: справочник KB, образец CSV и поведение импорта на некорректных строках."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from services import knowledge_base_service as kb_service
from services import review_pipeline
from services.ai_service import (
    SENTIMENT_VALUES,
    TOPIC_VALUES,
    AnalysisError,
    StructuredReviewAnalysis,
)
from services.csv_service import (
    REQUIRED_COLUMNS as CSV_REQUIRED_COLUMNS,
    RATING_MAX,
    RATING_MIN,
    CsvImportError,
    import_reviews_from_csv,
    read_csv_rows,
)
from services.knowledge_base_service import KnowledgeBaseError, load_knowledge_base
from services.localization_service import REVIEW_TYPE_RU, SENTIMENT_RU, TOPIC_RU
from services.report_service import average_rating
from services.review_service import list_reviews

_ROOT = Path(__file__).resolve().parent.parent
_KB_PATH = _ROOT / "data" / "knowledge_base.csv"
_SAMPLE_CSV = _ROOT / "samples" / "sample_reviews.csv"

_KB_HEADER = ",".join(kb_service.REQUIRED_COLUMNS) + "\n"
_CSV_HEADER = "customer_name,product_name,rating,review_text,source\n"


# --- справочник KB, поставляемый с проектом -----------------------------------------------------


def test_shipped_knowledge_base_has_required_columns() -> None:
    with _KB_PATH.open(encoding="utf-8-sig", newline="") as f:
        header = {name.strip() for name in (csv.DictReader(f).fieldnames or [])}

    assert set(kb_service.REQUIRED_COLUMNS) <= header


def test_shipped_knowledge_base_rows_are_valid() -> None:
    rows = load_knowledge_base(_KB_PATH)
    assert rows

    for row in rows:
        where = f"{row.topic}/{row.sentiment}: {row.common_phrase[:40]}"
        assert row.sentiment in SENTIMENT_VALUES, where
        assert row.topic in TOPIC_VALUES, where
        assert row.review_type in REVIEW_TYPE_RU, where
        for field in ("common_phrase", "reply_template", "recommended_action", "summary_example"):
            assert getattr(row, field), f"пустое поле {field}: {where}"

    # Подбор дедуплицирует по (topic, common_phrase, sentiment): дубль в файле молча исчезнет из выдачи.
    keys = [(r.topic, r.common_phrase, r.sentiment) for r in rows]
    assert len(keys) == len(set(keys))


def test_shipped_knowledge_base_covers_every_topic() -> None:
    covered = {row.topic for row in load_knowledge_base(_KB_PATH)}

    assert covered == set(TOPIC_VALUES)


def test_every_enum_value_has_a_russian_label() -> None:
    assert set(SENTIMENT_VALUES) == set(SENTIMENT_RU)
    assert set(TOPIC_VALUES) == set(TOPIC_RU)


# --- загрузчик KB на некорректном вводе ---------------------------------------------------------


def test_knowledge_base_missing_file_raises(tmp_path) -> None:
    with pytest.raises(KnowledgeBaseError, match="не найден"):
        load_knowledge_base(tmp_path / "нет.csv")


@pytest.mark.parametrize("missing", kb_service.REQUIRED_COLUMNS)
def test_knowledge_base_missing_column_raises(tmp_path, missing) -> None:
    columns = [c for c in kb_service.REQUIRED_COLUMNS if c != missing]
    path = tmp_path / "kb.csv"
    path.write_text(",".join(columns) + "\n" + ",".join("x" for _ in columns) + "\n", encoding="utf-8")

    with pytest.raises(KnowledgeBaseError, match=missing):
        load_knowledge_base(path)


def test_knowledge_base_skips_blank_and_topicless_rows_and_trims_values(tmp_path) -> None:
    path = tmp_path / "kb.csv"
    path.write_bytes(
        (
            _KB_HEADER
            + "complaint,  Долго ехал ,negative, delivery ,Шаблон,Действие,Сводка\n"
            + ",,,,,,\n"
            + "complaint,Фраза без темы,negative,,Шаблон,Действие,Сводка\n"
        ).encode("utf-8-sig")  # BOM, как у файла из Excel
    )

    (row,) = load_knowledge_base(path)

    assert (row.common_phrase, row.topic, row.sentiment) == ("Долго ехал", "delivery", "negative")


# --- образец CSV --------------------------------------------------------------------------------


def test_sample_csv_rows_are_importable() -> None:
    rows = read_csv_rows(_SAMPLE_CSV)

    assert len(rows) == 10
    for row in rows:
        assert row["review_text"] and row["source"]
        assert row["rating"] == "" or RATING_MIN <= int(row["rating"]) <= RATING_MAX


# --- импорт CSV на некорректном вводе -----------------------------------------------------------


def test_csv_missing_file_raises(tmp_path) -> None:
    with pytest.raises(CsvImportError, match="не найден"):
        read_csv_rows(tmp_path / "нет.csv")


@pytest.mark.parametrize("missing", CSV_REQUIRED_COLUMNS)
def test_csv_missing_column_raises(tmp_path, missing) -> None:
    columns = [c for c in CSV_REQUIRED_COLUMNS if c != missing]
    path = tmp_path / "reviews.csv"
    path.write_text(",".join(columns) + "\n" + ",".join("x" for _ in columns) + "\n", encoding="utf-8")

    with pytest.raises(CsvImportError, match=missing):
        read_csv_rows(path)


def test_csv_without_header_raises(tmp_path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(CsvImportError, match="заголовк"):
        read_csv_rows(path)


def test_csv_bom_and_whitespace_are_tolerated(tmp_path) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes(
        (_CSV_HEADER + "  Иван ,Товар,4,  Текст отзыва  ,csv_bom\n").encode("utf-8-sig")
    )

    (row,) = read_csv_rows(path)

    assert row["customer_name"] == "Иван"
    assert row["review_text"] == "Текст отзыва"


def test_malformed_rows_are_skipped_and_do_not_corrupt_the_average_rating(tmp_db, tmp_path) -> None:
    path = tmp_path / "mixed.csv"
    path.write_text(
        _CSV_HEADER
        + "Анна,Товар,1,Валидный отзыв с минимальной оценкой,csv_mixed\n"
        + "Борис,Товар,5,Валидный отзыв с максимальной оценкой,csv_mixed\n"
        + "Вера,Товар,,Валидный отзыв без оценки,csv_mixed\n"
        + "\n"
        + "Глеб,Товар,3,,csv_mixed\n"  # пустой текст
        + "Дина,Товар,3,Отзыв без источника,\n"  # пустой источник
        + "Егор,Товар,abc,Оценка не число,csv_mixed\n"
        + "Жанна,Товар,4.5,Оценка дробная,csv_mixed\n"
        + "Зоя,Товар,7,Оценка выше шкалы,csv_mixed\n"
        + "Иван,Товар,0,Оценка ниже шкалы,csv_mixed\n"
        + "Карл,Товар,-3,Оценка отрицательная,csv_mixed\n",
        encoding="utf-8",
    )

    stats = import_reviews_from_csv(tmp_db, path, process_with_ai=False)

    assert stats.rows_read == 10  # пустая строка не считается
    assert stats.rows_imported == 3
    assert stats.errors == 7
    assert stats.rows_skipped_duplicate == 0
    assert sorted(r["rating"] for r in list_reviews(tmp_db) if r["rating"] is not None) == [1, 5]
    assert average_rating(tmp_db) == 3.0


def test_csv_import_with_ai_requires_a_model(tmp_db, tmp_path) -> None:
    path = tmp_path / "reviews.csv"
    path.write_text(_CSV_HEADER + "Анна,Товар,4,Текст,csv_model\n", encoding="utf-8")

    with pytest.raises(CsvImportError, match="model"):
        import_reviews_from_csv(tmp_db, path, process_with_ai=True, api_key="k", model="  ")

    assert list_reviews(tmp_db) == []


def test_csv_import_with_ai_retries_failed_rows_and_skips_processed_ones(
    tmp_db, tmp_path, monkeypatch
) -> None:
    path = tmp_path / "reviews.csv"
    path.write_text(
        _CSV_HEADER
        + "Анна,Товар,4,Отзыв который обработается,csv_ai\n"
        + "Борис,Товар,2,Отзыв с первым сбоем модели,csv_ai\n",
        encoding="utf-8",
    )
    analysed: list[str] = []
    failing = {"Отзыв с первым сбоем модели"}

    def fake_analyze(**kwargs):
        text = kwargs["review_text"]
        analysed.append(text)
        if text in failing:
            raise AnalysisError("временный сбой")
        return StructuredReviewAnalysis("neutral", "other", "Кратко.", "Ответ.")

    monkeypatch.setattr(review_pipeline, "analyze_review", fake_analyze)

    def run():
        return import_reviews_from_csv(
            tmp_db, path, process_with_ai=True, api_key="test-key-not-real", model="test-model"
        )

    first = run()
    failing.clear()
    second = run()

    assert (first.rows_imported, first.rows_processed, first.processing_errors) == (2, 1, 1)
    assert (second.rows_imported, second.rows_skipped_duplicate) == (0, 2)
    assert (second.rows_processed, second.processing_errors) == (1, 0)
    # Второй запуск: обработанный отзыв модель не видит, неудавшийся уходит на повторный анализ.
    assert analysed == [
        "Отзыв который обработается",
        "Отзыв с первым сбоем модели",
        "Отзыв с первым сбоем модели",
    ]
    assert {r["status"] for r in list_reviews(tmp_db)} == {"processed"}
