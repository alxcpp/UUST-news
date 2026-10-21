"""SQLite connection management, schema setup, and local demo data."""

import json
import os
import sqlite3
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash


def get_db():
    """Return the SQLite connection cached for the current request."""
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    """Close the request-scoped connection when the app context ends."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the schema, apply additive migrations, and seed a local database."""
    database_path = Path(current_app.config["DATABASE"])
    database_path.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(database_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'moderator', 'admin')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL,
            image_url TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT 'article' CHECK (content_type IN ('article', 'event')),
            event_date TEXT,
            event_location TEXT,
            author_id INTEGER NOT NULL REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'published', 'rejected')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            published_at TEXT,
            reviewed_by INTEGER REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS likes (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            news_id INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, news_id)
        );
        CREATE TABLE IF NOT EXISTS comments (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            news_id INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
            body TEXT NOT NULL CHECK (length(body) <= 1200),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, news_id)
        );
        """
    )

    # Keep older local databases compatible without requiring a destructive rebuild.
    news_columns = {row[1] for row in db.execute("PRAGMA table_info(news)")}
    migrations = (
        ("content_type", "TEXT NOT NULL DEFAULT 'article' CHECK (content_type IN ('article', 'event'))"),
        ("event_date", "TEXT"),
        ("event_location", "TEXT"),
        ("grammar_result", "TEXT NOT NULL DEFAULT ''"),
    )
    for column, definition in migrations:
        if column not in news_columns:
            db.execute(f"ALTER TABLE news ADD COLUMN {column} {definition}")

    _clear_stale_ai_reports(db)
    _create_demo_content(db)
    db.commit()
    db.close()


def _clear_stale_ai_reports(db):
    """Remove legacy reports that do not contain a usable corrected draft."""
    rows = db.execute("SELECT id, grammar_result FROM news WHERE grammar_result != ''")
    for row in rows:
        try:
            report = json.loads(row["grammar_result"])
        except (TypeError, json.JSONDecodeError):
            report = None

        semantic = report.get("semantic") if isinstance(report, dict) else None
        if not isinstance(semantic, dict) or not semantic.get("corrected"):
            db.execute("UPDATE news SET grammar_result = '' WHERE id = ?", (row["id"],))


def _create_demo_content(db):
    """Create the initial administrator and demo articles for a new database."""
    admin_email = "admin@uust.local"
    admin = db.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
    if admin is not None:
        return

    cursor = db.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'admin')",
        (
            "Администратор УУНиТ",
            admin_email,
            generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin12345")),
        ),
    )
    admin_id = cursor.lastrowid
    demo_articles = [
        (
            "УУНиТ открывает новые горизонты",
            "Университет запускает программу поддержки студенческих инициатив и научных проектов.",
            "В Уфимском университете науки и технологий стартовала программа поддержки студенческих инициатив. "
            "Участники смогут получить консультации наставников, доступ к лабораториям и помощь в поиске "
            "партнёров для своих проектов.\n\nЗаявки принимаются от студентов всех институтов. Первые "
            "встречи с командами состоятся в главном корпусе университета.",
            "Университет",
            "https://images.unsplash.com/photo-1523050854058-8df90110c9f1?auto=format&fit=crop&w=1500&q=85",
        ),
        (
            "Студенты представили разработки на научном форуме",
            "Молодые исследователи УУНиТ показали проекты в области инженерии и цифровых технологий.",
            "Команды студентов УУНиТ выступили на межвузовском научном форуме. В программу вошли проекты "
            "по робототехнике, новым материалам и обработке данных.\n\nПо итогам форума несколько команд "
            "получили приглашения продолжить работу над проектами вместе с индустриальными партнёрами.",
            "Наука",
            "https://images.unsplash.com/photo-1531482615713-2afd69097998?auto=format&fit=crop&w=1200&q=85",
        ),
        (
            "Неделя культуры объединит университетские сообщества",
            "В кампусе подготовили открытые встречи, выставки и музыкальную программу.",
            "В университете пройдёт неделя культурных событий. Студенческие объединения подготовили выставку, "
            "открытые лекции и концертную программу.\n\nРасписание мероприятий будет опубликовано на "
            "страницах институтов. Участие открыто для студентов, сотрудников и гостей университета.",
            "Студентам",
            "https://images.unsplash.com/photo-1523580494863-6f3031224c94?auto=format&fit=crop&w=1200&q=85",
        ),
    ]

    for title, excerpt, content, category, image_url in demo_articles:
        db.execute(
            """INSERT INTO news
               (title, excerpt, content, category, image_url, author_id, status, published_at)
               VALUES (?, ?, ?, ?, ?, ?, 'published', CURRENT_TIMESTAMP)""",
            (title, excerpt, content, category, image_url, admin_id),
        )