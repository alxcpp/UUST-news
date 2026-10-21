"""Authentication, role checks, CSRF protection, and template helpers."""

import hmac
import json
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for

from .database import get_db


def current_user():
    """Load the signed-in user once per request."""
    if "user" not in g:
        user_id = session.get("user_id")
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
    return g.get("user")


def login_required(view):
    """Redirect anonymous visitors to sign in before calling a view."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Войдите, чтобы продолжить.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    """Restrict a view to authenticated users with one of the given roles."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user()["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def register_security(app):
    """Register request protection and shared template helpers on the app."""
    @app.before_request
    def protect_forms():
        if request.method != "POST":
            return

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

        return {
            "current_user": current_user(),
            "csrf_token": csrf_token,
            "now": datetime.now(timezone.utc),
        }

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
