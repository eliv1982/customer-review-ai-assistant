"""Импорт CSV без вызова OpenAI."""

from __future__ import annotations

from services.csv_service import import_reviews_from_csv


def test_csv_import_then_reimport_skips_duplicates(tmp_db: str, tmp_path) -> None:
    csv_file = tmp_path / "import_twice.csv"
    csv_file.write_text(
        "customer_name,product_name,rating,review_text,source\n"
        'Иван,Товар А,5,"Первый отзыв для импорта",pytest_csv\n'
        'Мария,Товар Б,,"Второй отзыв без рейтинга",pytest_csv\n',
        encoding="utf-8",
    )

    s1 = import_reviews_from_csv(tmp_db, csv_file, process_with_ai=False)
    assert s1.rows_read == 2
    assert s1.rows_imported == 2
    assert s1.rows_skipped_duplicate == 0

    s2 = import_reviews_from_csv(tmp_db, csv_file, process_with_ai=False)
    assert s2.rows_read == 2
    assert s2.rows_imported == 0
    assert s2.rows_skipped_duplicate == 2
