from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError


BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "instance" / "uploads"
MAX_IMAGE_SIZE = 8 * 1024 * 1024
IMAGE_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


class ImageUploadError(ValueError):
    pass


def save_uploaded_image(upload, upload_folder=None):
    if upload is None or not upload.filename:
        return ""

    content = upload.stream.read(MAX_IMAGE_SIZE + 1)
    if len(content) > MAX_IMAGE_SIZE:
        raise ImageUploadError("Размер изображения не должен превышать 8 МБ.")

    try:
        with Image.open(BytesIO(content)) as image:
            extension = IMAGE_EXTENSIONS.get(image.format)
            if extension is None:
                raise ImageUploadError("Поддерживаются только изображения JPG, PNG и WEBP.")
            image.verify()
    except ImageUploadError:
        raise
    except (Image.DecompressionBombError, OSError, SyntaxError, UnidentifiedImageError, ValueError) as error:
        raise ImageUploadError("Не удалось прочитать изображение. Выберите корректный JPG, PNG или WEBP.") from error

    destination = Path(upload_folder) if upload_folder else UPLOAD_DIR
    destination.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}{extension}"
    (destination / filename).write_bytes(content)
    return f"/uploads/{filename}"
