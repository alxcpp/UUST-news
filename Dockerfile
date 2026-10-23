FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/instance/uploads \
    && chown -R app:app /app

COPY --chown=app:app . .

USER app

EXPOSE 80

CMD ["gunicorn", "--preload", "--workers", "2", "--timeout", "75", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
