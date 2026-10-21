import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "local-development-key-change-before-deploying"),
    DATABASE=os.environ.get("DATABASE", str(INSTANCE_DIR / "uust_news.db")),
    LANGUAGETOOL_URL=os.environ.get("LANGUAGETOOL_URL", "https://api.languagetool.org/v2/check"),
    GRAMMAR_CHECK_TIMEOUT=6,
)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(app.config["DATABASE"])
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            news_id INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
            body TEXT NOT NULL CHECK (length(body) <= 1200),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    news_columns = {row[1] for row in db.execute("PRAGMA table_info(news)")}
    if "grammar_result" not in news_columns:
        db.execute("ALTER TABLE news ADD COLUMN grammar_result TEXT NOT NULL DEFAULT ''")

    admin_email = "admin@uust.local"
    admin = db.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
    if admin is None:
        cursor = db.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'admin')",
            (
                "Администратор УУНиТ",
                admin_email,
                generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin12345")),
            ),
        )
        admin_id = cursor.lastrowid
        demo_news = [
            (
                "УУНиТ открывает новые горизонты",
                "Университет запускает программу поддержки студенческих инициатив и научных проектов.",
                "В Уфимском университете науки и технологий стартовала программа поддержки студенческих инициатив. Участники смогут получить консультации наставников, доступ к лабораториям и помощь в поиске партнёров для своих проектов.\n\nЗаявки принимаются от студентов всех институтов. Первые встречи с командами состоятся в главном корпусе университета.",
                "Университет",
                "https://images.unsplash.com/photo-1523050854058-8df90110c9f1?auto=format&fit=crop&w=1500&q=85",
            ),
            (
                "Студенты представили разработки на научном форуме",
                "Молодые исследователи УУНиТ показали проекты в области инженерии и цифровых технологий.",
                "Команды студентов УУНиТ выступили на межвузовском научном форуме. В программу вошли проекты по робототехнике, новым материалам и обработке данных.\n\nПо итогам форума несколько команд получили приглашения продолжить работу над проектами вместе с индустриальными партнёрами.",
                "Наука",
                "https://images.unsplash.com/photo-1531482615713-2afd69097998?auto=format&fit=crop&w=1200&q=85",
            ),
            (
                "Неделя культуры объединит университетские сообщества",
                "В кампусе подготовили открытые встречи, выставки и музыкальную программу.",
                "В университете пройдёт неделя культурных событий. Студенческие объединения подготовили выставку, открытые лекции и концертную программу.\n\nРасписание мероприятий будет опубликовано на страницах институтов. Участие открыто для студентов, сотрудников и гостей университета.",
                "Студентам",
                "https://images.unsplash.com/photo-1523580494863-6f3031224c94?auto=format&fit=crop&w=1200&q=85",
            ),
        ]
        for title, excerpt, content, category, image_url in demo_news:
            db.execute(
                """INSERT INTO news
                   (title, excerpt, content, category, image_url, author_id, status, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'published', CURRENT_TIMESTAMP)""",
                (title, excerpt, content, category, image_url, admin_id),
            )
    db.commit()
    db.close()


def current_user():
    if "user" not in g:
        user_id = session.get("user_id")
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
    return g.get("user")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Войдите, чтобы продолжить.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user()["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


@app.before_request
def protect_forms():
    if request.method == "POST":
        submitted = request.form.get("csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not expected or not hmac.compare_digest(submitted, expected):
            abort(400, description="Форма устарела. Обновите страницу и попробуйте снова.")


@app.context_processor
def inject_template_values():
    def csrf_token():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(32)
        return session["_csrf_token"]

    return {"current_user": current_user(), "csrf_token": csrf_token, "now": datetime.now(timezone.utc)}


@app.template_filter("date_ru")
def date_ru(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return value


@app.template_filter("from_json")
def from_json(value):
    try:
        return json.loads(value) if value else None
    except (TypeError, json.JSONDecodeError):
        return None


def check_grammar(title, excerpt, content):
    sections = [("title", title), ("excerpt", excerpt), ("content", content)]
    spans = []
    chunks = []
    offset = 0
    for field, text in sections:
        if chunks:
            chunks.append("\n\n")
            offset += 2
        spans.append((field, text, offset, offset + len(text)))
        chunks.append(text)
        offset += len(text)
    text = "".join(chunks)
    body = urlencode({"text": text, "language": "ru-RU"}).encode("utf-8")
    check_request = Request(
        app.config["LANGUAGETOOL_URL"],
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(check_request, timeout=app.config["GRAMMAR_CHECK_TIMEOUT"]) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, KeyError, TypeError):
        return {"status": "unavailable", "matches": []}

    matches = []
    edits_by_field = {field: [] for field, _ in sections}
    for match in payload.get("matches", [])[:50]:
        match_offset = match.get("offset", 0)
        section = next(
            (item for item in spans if item[2] <= match_offset <= item[3]),
            None,
        )
        if section is None:
            continue
        field, section_text, start, end = section
        local_offset = max(0, min(match_offset - start, len(section_text)))
        match_length = max(0, min(match.get("length", 0), len(section_text) - local_offset))
        context_start = max(0, local_offset - 35)
        context_end = min(len(section_text), local_offset + match_length + 35)
        suggestions = [item["value"] for item in match.get("replacements", [])[:5] if item.get("value")]
        matches.append(
            {
                "field": field,
                "fragment": section_text[context_start:context_end],
                "message": match.get("message", "Возможная ошибка"),
                "suggestions": suggestions,
            }
        )
        if suggestions and match_length:
            edits_by_field[field].append((local_offset, match_length, suggestions[0]))

    corrected = {}
    originals = dict(sections)
    for field, original in sections:
        revised = original
        last_edit_start = len(original) + 1
        for local_offset, match_length, replacement in sorted(edits_by_field[field], reverse=True):
            if local_offset + match_length <= last_edit_start:
                revised = revised[:local_offset] + replacement + revised[local_offset + match_length:]
                last_edit_start = local_offset
        corrected[field] = revised
    return {"status": "checked", "matches": matches, "corrected": corrected}


@app.route("/")
def index():
    db = get_db()
    category = request.args.get("category", "")
    search = request.args.get("q", "").strip()
    filters = ["status = 'published'"]
    params = []
    if category:
        filters.append("category = ?")
        params.append(category)
    if search:
        filters.append("(title LIKE ? OR excerpt LIKE ? OR content LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    where = " AND ".join(filters)
    articles = db.execute(
        f"""SELECT news.*, users.name AS author_name,
                   (SELECT COUNT(*) FROM likes WHERE news_id = news.id) AS like_count,
                   (SELECT COUNT(*) FROM comments WHERE news_id = news.id) AS comment_count
            FROM news JOIN users ON users.id = news.author_id
            WHERE {where} ORDER BY published_at DESC, id DESC""",
        params,
    ).fetchall()
    categories = db.execute(
        "SELECT DISTINCT category FROM news WHERE status = 'published' ORDER BY category"
    ).fetchall()
    return render_template("index.html", articles=articles, categories=categories, category=category, search=search)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if len(name) < 2 or len(name) > 80 or "@" not in email or len(password) < 8:
            flash("Проверьте имя, email и пароль (не менее 8 символов).", "error")
        else:
            try:
                db = get_db()
                db.execute(
                    "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                    (name, email, generate_password_hash(password)),
                )
                db.commit()
                user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                session.clear()
                session["user_id"] = user["id"]
                flash("Аккаунт создан. Добро пожаловать!", "success")
                return redirect(url_for("index"))
            except sqlite3.IntegrityError:
                flash("Аккаунт с таким email уже существует.", "error")
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Неверный email или пароль.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            target = request.args.get("next", "")
            return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("index"))
    return render_template("auth.html", mode="login")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("Вы вышли из аккаунта.", "info")
    return redirect(url_for("index"))


@app.post("/proposals")
@login_required
def submit_proposal():
    title = request.form.get("title", "").strip()
    excerpt = request.form.get("excerpt", "").strip()
    content = request.form.get("content", "").strip()
    category = request.form.get("category", "Университет").strip()
    image_url = request.form.get("image_url", "").strip()
    allowed_categories = {"Университет", "Наука", "Студентам", "Кампус"}
    valid_image = not image_url or image_url.startswith(("https://", "http://"))
    if not (4 <= len(title) <= 150 and 10 <= len(excerpt) <= 260 and 30 <= len(content) <= 8000):
        flash("Заполните заголовок, краткое описание и текст новости.", "error")
    elif category not in allowed_categories or not valid_image:
        flash("Проверьте категорию и ссылку на изображение.", "error")
    else:
        db = get_db()
        grammar_result = check_grammar(title, excerpt, content)
        db.execute(
            """INSERT INTO news (title, excerpt, content, category, image_url, author_id, grammar_result)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                excerpt,
                content,
                category,
                image_url,
                current_user()["id"],
                json.dumps(grammar_result, ensure_ascii=False),
            ),
        )
        db.commit()
        if grammar_result["status"] == "unavailable":
            flash("Предложение отправлено. Автопроверка временно недоступна, редакция проверит текст вручную.", "info")
        elif grammar_result["matches"]:
            flash(
                f"Предложение отправлено. Автопроверка нашла замечания: {len(grammar_result['matches'])}.",
                "info",
            )
        else:
            flash("Предложение отправлено. Автопроверка не нашла замечаний.", "success")
    return redirect(url_for("index"))


@app.route("/news/<int:news_id>")
def news_detail(news_id):
    db = get_db()
    article = db.execute(
        """SELECT news.*, users.name AS author_name,
                  (SELECT COUNT(*) FROM likes WHERE news_id = news.id) AS like_count
           FROM news JOIN users ON users.id = news.author_id
           WHERE news.id = ? AND news.status = 'published'""",
        (news_id,),
    ).fetchone()
    if article is None:
        abort(404)
    comments = db.execute(
        """SELECT comments.*, users.name AS author_name FROM comments
           JOIN users ON users.id = comments.user_id WHERE news_id = ?
           ORDER BY comments.created_at DESC""",
        (news_id,),
    ).fetchall()
    liked = False
    if current_user():
        liked = db.execute(
            "SELECT 1 FROM likes WHERE user_id = ? AND news_id = ?",
            (current_user()["id"], news_id),
        ).fetchone() is not None
    return render_template("news.html", article=article, comments=comments, liked=liked)


@app.post("/news/<int:news_id>/like")
@login_required
def toggle_like(news_id):
    db = get_db()
    if db.execute("SELECT 1 FROM news WHERE id = ? AND status = 'published'", (news_id,)).fetchone() is None:
        abort(404)
    existing = db.execute(
        "SELECT 1 FROM likes WHERE user_id = ? AND news_id = ?", (current_user()["id"], news_id)
    ).fetchone()
    if existing:
        db.execute("DELETE FROM likes WHERE user_id = ? AND news_id = ?", (current_user()["id"], news_id))
    else:
        db.execute("INSERT INTO likes (user_id, news_id) VALUES (?, ?)", (current_user()["id"], news_id))
    db.commit()
    return redirect(url_for("news_detail", news_id=news_id))


@app.post("/news/<int:news_id>/comments")
@login_required
def add_comment(news_id):
    body = request.form.get("body", "").strip()
    db = get_db()
    if db.execute("SELECT 1 FROM news WHERE id = ? AND status = 'published'", (news_id,)).fetchone() is None:
        abort(404)
    if not body or len(body) > 1200:
        flash("Комментарий должен содержать от 1 до 1200 символов.", "error")
    else:
        db.execute(
            "INSERT INTO comments (user_id, news_id, body) VALUES (?, ?, ?)",
            (current_user()["id"], news_id, body),
        )
        db.commit()
    return redirect(url_for("news_detail", news_id=news_id) + "#comments")


@app.route("/moderation")
@roles_required("moderator", "admin")
def moderation():
    db = get_db()
    pending = db.execute(
        """SELECT news.*, users.name AS author_name, users.email AS author_email
           FROM news JOIN users ON users.id = news.author_id
           WHERE news.status = 'pending' ORDER BY news.created_at ASC"""
    ).fetchall()
    users = []
    published = []
    if current_user()["role"] == "admin":
        users = db.execute(
            "SELECT id, name, email, role, created_at FROM users WHERE role != 'admin' ORDER BY created_at DESC"
        ).fetchall()
        published = db.execute(
            "SELECT id, title, category, published_at FROM news WHERE status = 'published' ORDER BY published_at DESC"
        ).fetchall()
    return render_template("moderation.html", pending=pending, users=users, published=published)


@app.route("/moderation/<int:news_id>/edit", methods=["GET", "POST"])
@roles_required("moderator", "admin")
def edit_proposal(news_id):
    db = get_db()
    proposal = db.execute(
        "SELECT * FROM news WHERE id = ? AND status = 'pending'",
        (news_id,),
    ).fetchone()
    if proposal is None:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        excerpt = request.form.get("excerpt", "").strip()
        content = request.form.get("content", "").strip()
        category = request.form.get("category", "").strip()
        image_url = request.form.get("image_url", "").strip()
        allowed_categories = {"Университет", "Наука", "Студентам", "Кампус"}
        valid_text = 4 <= len(title) <= 150 and 10 <= len(excerpt) <= 260 and 30 <= len(content) <= 8000
        valid_image = not image_url or image_url.startswith(("https://", "http://"))
        if not valid_text:
            flash("Проверьте заголовок, краткое описание и текст новости.", "error")
        elif category not in allowed_categories or not valid_image:
            flash("Проверьте категорию и ссылку на изображение.", "error")
        else:
            grammar_result = check_grammar(title, excerpt, content)
            cursor = db.execute(
                """UPDATE news SET title = ?, excerpt = ?, content = ?, category = ?, image_url = ?, grammar_result = ?
                   WHERE id = ? AND status = 'pending'""",
                (
                    title,
                    excerpt,
                    content,
                    category,
                    image_url,
                    json.dumps(grammar_result, ensure_ascii=False),
                    news_id,
                ),
            )
            db.commit()
            if cursor.rowcount:
                flash("Предложение обновлено. Результат проверки грамматики обновлён.", "success")
            else:
                flash("Предложение уже обработано и больше не доступно для редактирования.", "info")
            return redirect(url_for("moderation"))
        return render_template("proposal_edit.html", article=proposal, values=request.form)

    return render_template("proposal_edit.html", article=proposal, values={})


@app.post("/moderation/<int:news_id>/<action>")
@roles_required("moderator", "admin")
def review_proposal(news_id, action):
    if action not in {"approve", "reject"}:
        abort(404)
    db = get_db()
    status = "published" if action == "approve" else "rejected"
    published_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if action == "approve" else None
    cursor = db.execute(
        """UPDATE news SET status = ?, published_at = ?, reviewed_by = ?
           WHERE id = ? AND status = 'pending'""",
        (status, published_at, current_user()["id"], news_id),
    )
    db.commit()
    if cursor.rowcount:
        flash("Новость опубликована." if action == "approve" else "Предложение отклонено.", "success")
    else:
        flash("Это предложение уже обработано.", "info")
    return redirect(url_for("moderation"))


@app.post("/admin/users/<int:user_id>/role")
@roles_required("admin")
def update_role(user_id):
    role = request.form.get("role")
    if role not in {"user", "moderator"}:
        abort(400)
    if user_id == current_user()["id"]:
        flash("Нельзя изменить собственную роль администратора.", "error")
        return redirect(url_for("moderation") + "#team")
    db = get_db()
    cursor = db.execute("UPDATE users SET role = ? WHERE id = ? AND role != 'admin'", (role, user_id))
    db.commit()
    flash("Роль пользователя обновлена." if cursor.rowcount else "Пользователь не найден.", "success")
    return redirect(url_for("moderation") + "#team")


@app.post("/admin/news/<int:news_id>/remove")
@roles_required("admin")
def remove_news(news_id):
    db = get_db()
    cursor = db.execute(
        "UPDATE news SET status = 'rejected', reviewed_by = ? WHERE id = ? AND status = 'published'",
        (current_user()["id"], news_id),
    )
    db.commit()
    flash("Публикация снята с сайта." if cursor.rowcount else "Опубликованная новость не найдена.", "info")
    return redirect(url_for("moderation"))


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="У вас нет доступа к этому разделу."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="Похоже, этой страницы больше нет."), 404


init_db()


if __name__ == "__main__":
    app.run(debug=True)