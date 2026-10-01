"""Стабильный путь к SQLite: не зависит от текущего рабочего каталога процесса."""

from __future__ import annotations

from pathlib import Path

import config
from services.review_service import init_database


def test_relative_database_path_resolves_under_project_root(monkeypatch, tmp_path) -> None:
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    default_path = config.get_settings().database_path
    assert Path(default_path) == config.PROJECT_ROOT / "data" / "reviews.db"

    monkeypatch.setenv("DATABASE_PATH", "data/custom.db")
    assert Path(config.get_settings().database_path) == config.PROJECT_ROOT / "data" / "custom.db"

    # Разрешение пути ничего не создаёт в чужом cwd.
    assert list(other_cwd.iterdir()) == []


def test_relative_database_path_is_same_from_repo_root_and_other_cwd(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_PATH", "data/reviews.db")
    expected = str(config.PROJECT_ROOT / "data" / "reviews.db")

    monkeypatch.chdir(config.PROJECT_ROOT)
    from_repo_root = config.get_settings().database_path
    monkeypatch.chdir(tmp_path)
    from_other_cwd = config.get_settings().database_path

    assert from_repo_root == from_other_cwd == expected


def test_absolute_database_path_is_preserved(monkeypatch, tmp_path) -> None:
    absolute = tmp_path / "abs" / "my_reviews.db"
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.setenv("DATABASE_PATH", str(absolute))

    assert config.get_settings().database_path == str(absolute)


def test_init_database_does_not_create_second_db_in_other_cwd(monkeypatch, tmp_path) -> None:
    # Подменяем корень проекта, чтобы тест не трогал боевую data/reviews.db.
    fake_root = tmp_path / "project_root"
    other_cwd = tmp_path / "elsewhere"
    fake_root.mkdir()
    other_cwd.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", fake_root)
    monkeypatch.chdir(other_cwd)
    monkeypatch.setenv("DATABASE_PATH", "data/reviews.db")

    init_database(config.get_settings().database_path)

    assert (fake_root / "data" / "reviews.db").is_file()
    assert list(other_cwd.iterdir()) == []
