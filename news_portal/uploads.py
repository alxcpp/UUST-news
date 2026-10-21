"""Validation, storage, and access control for news images."""

import os
import secrets
from pathlib import Path

from flask import abort, current_app, send_from_directory, url_for
from PIL import Image, ImageOps, UnidentifiedImageError

from .database import get_db
from .security import current_user


MAX_IMAGE_SIZE = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
IMAGE_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


def save_uploaded_image(upload):
    """Validate and normalize an uploaded image, then return its local URL."""
    if upload is None or not upload.filename:
        return "", None

    destination = None
    try:
        upload.stream.seek(0, os.SEEK_END)
        file_size = upload.stream.tell()
        upload.stream.seek(0)
        if file_size > MAX_IMAGE_SIZE:
            return None, "Изображение должно быть не больше 8 МБ."

        with Image.open(upload.stream) as source:
            image_format = source.format
            if image_format not in IMAGE_FORMATS:
                return None, "Поддерживаются изображения JPG, PNG и WEBP."
            if source.width * source.height > MAX_IMAGE_PIXELS:
                return None, "Размеры изображения слишком велики."
            if getattr(source, "n_frames", 1) > 1:
                return None, "Загрузите статичное изображение, не анимацию."
            source.verify()

        upload.stream.seek(0)
        with Image.open(upload.stream) as source:
            image = ImageOps.exif_transpose(source)
            if image_format == "JPEG" and image.mode != "RGB":
                image = image.convert("RGB")
            elif image_format == "PNG" and image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                image = image.convert("RGBA")
            elif image_format == "WEBP" and image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

            filename = f"{secrets.token_hex(16)}.{IMAGE_FORMATS[image_format]}"
            upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
            upload_folder.mkdir(parents=True, exist_ok=True)
            destination = upload_folder / filename
            image.save(destination, format=image_format)

        return url_for("uploaded_image", filename=filename), None
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        if destination is not None:
            destination.unlink(missing_ok=True)
        return None, "Не удалось прочитать изображение. Загрузите корректный JPG, PNG или WEBP."


def register_upload_routes(app):
    """Register the endpoint that serves images according to news visibility."""
    @app.get("/uploads/<filename>")
    def uploaded_image(filename):
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        stem = filename.rsplit(".", 1)[0] if "." in filename else ""
        if extension not in IMAGE_FORMATS.values() or len(stem) != 32 or any(
            character not in "0123456789abcdef" for character in stem
        ):
            abort(404)

        images = get_db().execute(
            "SELECT status FROM news WHERE image_url LIKE ?",
            (f"%/uploads/{filename}",),
        ).fetchall()
        if not images:
            abort(404)

        # Pending and rejected submissions are visible only to the editorial team.
        if not any(image["status"] == "published" for image in images):
            user = current_user()
            if user is None or user["role"] not in {"moderator", "admin"}:
                abort(404)

        return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)
