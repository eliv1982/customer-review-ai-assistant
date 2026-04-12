"""

Импорт отзывов из CSV в SQLite с опциональным запуском пайплайна ИИ.



Кодировка файла: UTF-8. Зависимости: только стандартный модуль ``csv``.



Режим ``process_with_ai=True``: сначала ``add_review`` (одна строка на CSV-строку),

затем ``process_existing_review`` по полученному ``review_id`` — **без** повторной вставки.

Повторный импорт того же содержимого (тот же ``source`` + ``review_text``): новая строка

в ``reviews`` **не** создаётся; при ``process_with_ai`` необработанные строки догоняются ИИ.

"""

from __future__ import annotations



import csv

import logging

from dataclasses import dataclass

from pathlib import Path

from typing import Sequence



from services.review_pipeline import process_existing_review

from services.review_service import add_review, find_review_by_source_and_text



logger = logging.getLogger(__name__)



# Ожидаемые имена колонок в первой строке файла (регистр должен совпадать).

REQUIRED_COLUMNS: tuple[str, ...] = (

    "customer_name",

    "product_name",

    "rating",

    "review_text",

    "source",

)





class CsvImportError(Exception):

    """Ошибка формата CSV или обязательных колонок."""





@dataclass(frozen=True)

class CsvImportStats:

    """Сводка по одному запуску импорта."""



    rows_read: int

    rows_imported: int

    rows_skipped_duplicate: int

    rows_processed: int

    processing_errors: int

    errors: int





def _parse_rating(value: str | None) -> int | None:

    if value is None:

        return None

    s = str(value).strip()

    if not s:

        return None

    try:

        return int(s)

    except ValueError as e:

        raise ValueError(f"Некорректный rating: {value!r}") from e





def read_csv_rows(csv_path: str | Path) -> list[dict[str, str]]:

    """

    Читает CSV целиком в память (список словарей по строкам).



    Используется UTF-8; при необходимости поддержите UTF-8 BOM в редакторе или

    замените на ``utf-8-sig`` при чтении.

    """

    path = Path(csv_path)

    if not path.is_file():

        raise CsvImportError(f"Файл не найден: {path}")



    rows: list[dict[str, str]] = []

    # utf-8-sig: тот же UTF-8, но корректно снимает BOM, если файл сохранён из Excel.

    with path.open(encoding="utf-8-sig", newline="") as f:

        reader = csv.DictReader(f)

        validate_columns(reader.fieldnames)

        for raw in reader:

            # Пустые строки (все поля пустые) пропускаем.

            if not any((v or "").strip() for v in raw.values()):

                continue

            rows.append({k: (raw.get(k) or "").strip() for k in REQUIRED_COLUMNS})

    return rows





def validate_columns(fieldnames: Sequence[str] | None) -> None:

    """Проверяет, что в заголовке есть все обязательные колонки."""

    if not fieldnames:

        raise CsvImportError("В CSV нет строки заголовка или она пуста.")

    names = {fn.strip() for fn in fieldnames if fn}

    missing = [c for c in REQUIRED_COLUMNS if c not in names]

    if missing:

        raise CsvImportError(f"В заголовке CSV не хватает колонок: {missing}")





def import_reviews_from_csv(

    database_path: str,

    csv_path: str | Path,

    *,

    process_with_ai: bool = False,

    api_key: str | None = None,

    model: str | None = None,

) -> CsvImportStats:

    """

    Импортирует отзывы из CSV в таблицу ``reviews``.



    Если ``process_with_ai=True``:

    для каждой строки выполняется **одна** вставка ``add_review`` (если такого

    ``source`` + ``review_text`` ещё нет), затем ``process_existing_review(review_id=...)``.

    Уже существующая запись с тем же текстом и источником не дублируется в ``reviews``.



    ``api_key`` и ``model`` обязательны при ``process_with_ai=True`` (можно передать

    из ``get_settings()``).

    """

    if process_with_ai and (not model or not str(model).strip()):

        raise CsvImportError("При process_with_ai=True нужен непустой model")



    rows = read_csv_rows(csv_path)

    rows_read = len(rows)

    imported = 0

    skipped_duplicate = 0

    processed = 0

    processing_errors = 0

    errors = 0



    logger.info(

        "Импорт CSV: файл=%s, строк=%s, process_with_ai=%s "

        "(ИИ: add_review при отсутствии дубликата + process_existing_review)",

        csv_path,

        rows_read,

        process_with_ai,

    )



    for idx, row in enumerate(rows, start=2):

        # start=2: строка 1 в файле — заголовок; первая data — логически «строка 2».

        line_no = idx

        try:

            review_text = row["review_text"].strip()

            source = row["source"].strip()

            if not review_text or not source:

                raise ValueError("Поля review_text и source не могут быть пустыми")



            customer = row["customer_name"].strip() or None

            product = row["product_name"].strip() or None

            rating = _parse_rating(row.get("rating"))



            existing = find_review_by_source_and_text(

                database_path,

                source=source,

                review_text=review_text,

            )

            if existing is None:

                review_id = add_review(

                    database_path,

                    source=source,

                    review_text=review_text,

                    customer_name=customer,

                    product_name=product,

                    rating=rating,

                    status="new",

                )

                imported += 1

                logger.info(

                    "CSV строка %s: импортирован отзыв id=%s (одна запись в reviews)",

                    line_no,

                    review_id,

                )

                prev_status = "new"

            else:

                review_id, prev_status = existing

                skipped_duplicate += 1

                logger.info(

                    "CSV строка %s: дубликат (source+review_text) — уже есть id=%s "

                    "(status=%s), повторная вставка не выполняется",

                    line_no,

                    review_id,

                    prev_status,

                )



            if process_with_ai:

                if existing is not None and prev_status == "processed":

                    logger.info(

                        "CSV строка %s: отзыв id=%s уже processed, ИИ не вызывается",

                        line_no,

                        review_id,

                    )

                else:

                    outcome = process_existing_review(

                        database_path,

                        review_id,

                        api_key=api_key,

                        model=str(model).strip(),

                    )

                    if outcome.status == "processed":

                        processed += 1

                        logger.info(

                            "CSV строка %s: отзыв id=%s успешно обработан (OpenAI + analysis)",

                            line_no,

                            review_id,

                        )

                    else:

                        processing_errors += 1

                        logger.warning(

                            "CSV строка %s: отзыв id=%s, обработка завершилась со статусом error",

                            line_no,

                            review_id,

                        )

        except Exception as exc:

            errors += 1

            logger.warning("Строка CSV %s: пропуск из-за ошибки: %s", line_no, exc)



    stats = CsvImportStats(

        rows_read=rows_read,

        rows_imported=imported,

        rows_skipped_duplicate=skipped_duplicate,

        rows_processed=processed,

        processing_errors=processing_errors,

        errors=errors,

    )

    logger.info(

        "Импорт CSV завершён: прочитано=%s, импортировано(новых строк)=%s, "

        "пропущено_дубликатов=%s, обработано(processed)=%s, "

        "ошибок_обработки=%s, ошибок_строки=%s",

        stats.rows_read,

        stats.rows_imported,

        stats.rows_skipped_duplicate,

        stats.rows_processed,

        stats.processing_errors,

        stats.errors,

    )

    return stats


