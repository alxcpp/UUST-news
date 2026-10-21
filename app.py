"""Flask entry point and HTTP route handlers for the news portal."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from news_portal.database import close_db, get_db, init_db
from news_portal.gemini import check_text
from news_portal.security import current_user, login_required, register_security, roles_required
from news_portal.uploads import register_upload_routes, save_uploaded_image


# Application configuration and shared service registration.
BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "local-development-key-change-before-deploying"),
    DATABASE=os.environ.get("DATABASE", str(INSTANCE_DIR / "uust_news.db")),
    MAX_CONTENT_LENGTH=9 * 1024 * 1024,
    UPLOAD_FOLDER=str(INSTANCE_DIR / "uploads"),
    GEMINI_API_KEY=os.environ.get("GEMINI_API_KEY", ""),
    GEMINI_MODEL=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
    GEMINI_FALLBACK_MODELS=os.environ.get(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash-lite,gemini-3-flash-preview,gemini-3.5-flash",
    ),
    GEMINI_API_URL=os.environ.get(
        "GEMINI_API_URL",
        "https://generativelanguage.googleapis.com/v1beta/models",
    ),
    GEMINI_TIMEOUT=45,
)

app.teardown_appcontext(close_db)
register_security(app)
register_upload_routes(app)


# Public routes: feed, account access, proposals, and reader interactions.


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
    content_type = request.form.get("content_type", "article")
    event_date = request.form.get("event_date", "").strip()
    event_location = request.form.get("event_location", "").strip()
    allowed_categories = {"Университет", "Наука", "Студентам", "Кампус", "Мероприятия"}
    valid_type = content_type in {"article", "event"}
    valid_event = True
    if content_type == "event":
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError:
            valid_event = False
        valid_event = valid_event and 2 <= len(event_location) <= 160
    if not (4 <= len(title) <= 150 and 10 <= len(excerpt) <= 260 and 30 <= len(content) <= 8000):
        flash("Заполните заголовок, краткое описание и текст новости.", "error")
    elif not valid_type or not valid_event:
        flash("Для мероприятия укажите корректные дату и место проведения.", "error")
    elif category not in allowed_categories:
        flash("Проверьте выбранный раздел.", "error")
    else:
        image_url, image_error = save_uploaded_image(request.files.get("image_file"))
        if image_error:
            flash(image_error, "error")
            return redirect(url_for("index"))
        db = get_db()
        grammar_result = check_text(title, excerpt, content)
        db.execute(
            """INSERT INTO news
               (title, excerpt, content, category, image_url, content_type, event_date, event_location, author_id, grammar_result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                excerpt,
                content,
                category,
                image_url,
                content_type,
                event_date if content_type == "event" else None,
                event_location if content_type == "event" else None,
                current_user()["id"],
                json.dumps(grammar_result, ensure_ascii=False),
            ),
        )
        db.commit()
        semantic = grammar_result["semantic"]
        if semantic["status"] == "unavailable":
            reason = semantic.get("reason")
            messages = {
                "not_configured": "Ключ Gemini API не настроен.",
                "invalid_key": "Gemini API отклонил ключ или доступ к модели.",
                "quota_exceeded": "Исчерпан лимит запросов Gemini API.",
                "service_busy": "Модели Gemini временно перегружены. Попробуйте отправить новость ещё раз позже.",
                "connection_error": "Нет соединения с Gemini API.",
                "model_unavailable": "Настроенные модели Gemini недоступны для этого ключа.",
            }
            flash(f"Предложение сохранено без AI-проверки. {messages.get(reason, 'Проверьте доступность Gemini API.')}", "info")
        elif any(
            category["status"] in {"issues", "uncertain"} for category in semantic["categories"]
        ):
            flash(
                "Предложение отправлено. В очереди доступны результаты проверок и замечания ИИ.",
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


# Editorial routes: review pending proposals and manage published articles.
@app.route("/moderation")
@roles_required("moderator", "admin")
def moderation():
    db = get_db()
    user_email = request.args.get("user_email", "").strip()
    pending = db.execute(
        """SELECT news.*, users.name AS author_name, users.email AS author_email
           FROM news JOIN users ON users.id = news.author_id
           WHERE news.status = 'pending' ORDER BY news.created_at ASC"""
    ).fetchall()
    users = []
    published = []
    if current_user()["role"] == "admin":
        if user_email:
            users = db.execute(
                """SELECT id, name, email, role, created_at FROM users
                   WHERE id != ? AND email LIKE ? COLLATE NOCASE ORDER BY created_at DESC""",
                (current_user()["id"], f"%{user_email}%"),
            ).fetchall()
        else:
            users = db.execute(
                "SELECT id, name, email, role, created_at FROM users WHERE id != ? ORDER BY created_at DESC",
                (current_user()["id"],),
            ).fetchall()
        published = db.execute(
            "SELECT id, title, category, published_at FROM news WHERE status = 'published' ORDER BY published_at DESC"
        ).fetchall()
    return render_template(
        "moderation.html", pending=pending, users=users, published=published, user_email=user_email
    )


@app.route("/moderation/<int:news_id>/edit", methods=["GET", "POST"])
@roles_required("moderator", "admin")
def edit_proposal(news_id):
    db = get_db()
    proposal = db.execute(
        "SELECT * FROM news WHERE id = ? AND status IN ('pending', 'published')",
        (news_id,),
    ).fetchone()
    # Moderators edit pending submissions; only administrators can edit published news.
    if proposal is None or (proposal["status"] == "published" and current_user()["role"] != "admin"):
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        excerpt = request.form.get("excerpt", "").strip()
        content = request.form.get("content", "").strip()
        category = request.form.get("category", "").strip()
        content_type = request.form.get("content_type", proposal["content_type"])
        event_date = request.form.get("event_date", "").strip()
        event_location = request.form.get("event_location", "").strip()
        allowed_categories = {"Университет", "Наука", "Студентам", "Кампус", "Мероприятия"}
        valid_text = 4 <= len(title) <= 150 and 10 <= len(excerpt) <= 260 and 30 <= len(content) <= 8000
        valid_type = content_type in {"article", "event"}
        valid_event = True
        if content_type == "event":
            try:
                datetime.strptime(event_date, "%Y-%m-%d")
            except ValueError:
                valid_event = False
            valid_event = valid_event and 2 <= len(event_location) <= 160
        if not valid_text:
            flash("Проверьте заголовок, краткое описание и текст новости.", "error")
        elif not valid_type or not valid_event:
            flash("Для мероприятия укажите корректные дату и место проведения.", "error")
        elif category not in allowed_categories:
            flash("Проверьте выбранный раздел.", "error")
        else:
            image_url = proposal["image_url"]
            uploaded_image = request.files.get("image_file")
            if uploaded_image and uploaded_image.filename:
                image_url, image_error = save_uploaded_image(uploaded_image)
                if image_error:
                    flash(image_error, "error")
                    return render_template("proposal_edit.html", article=proposal, values=request.form), 400
            grammar_result = check_text(title, excerpt, content)
            cursor = db.execute(
                """UPDATE news SET title = ?, excerpt = ?, content = ?, category = ?, image_url = ?,
                   content_type = ?, event_date = ?, event_location = ?, grammar_result = ?
                   WHERE id = ? AND status = ?""",
                (
                    title,
                    excerpt,
                    content,
                    category,
                    image_url,
                    content_type,
                    event_date if content_type == "event" else None,
                    event_location if content_type == "event" else None,
                    json.dumps(grammar_result, ensure_ascii=False),
                    news_id,
                    proposal["status"],
                ),
            )
            db.commit()
            if cursor.rowcount:
                flash(
                    "Опубликованная новость обновлена." if proposal["status"] == "published"
                    else "Предложение обновлено. Результат проверки грамматики обновлён.",
                    "success",
                )
            else:
                flash("Новость уже изменилась и больше недоступна для редактирования.", "info")
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


# Administrator-only routes.
@app.post("/admin/users/<int:user_id>/role")
@roles_required("admin")
def update_role(user_id):
    role = request.form.get("role")
    if role not in {"user", "moderator", "admin"}:
        abort(400)
    if user_id == current_user()["id"]:
        flash("Нельзя изменить собственную роль администратора.", "error")
        return redirect(url_for("moderation") + "#team")
    db = get_db()
    cursor = db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
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


# Consistent user-facing responses for common request errors.
@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="У вас нет доступа к этому разделу."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="Похоже, этой страницы больше нет."), 404


@app.errorhandler(413)
def upload_too_large(_error):
    return render_template("error.html", code=413, message="Размер отправленной формы превышает допустимый лимит. Максимальный размер изображения — 8 МБ."), 413


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True)