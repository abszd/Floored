# The Floored API. Build from the repo root:  docker build -t floored .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FLOORED_DATA_DIR=/data
WORKDIR /app

# Copies backend/ and, when present, the rebuilt converter/ (see .dockerignore).
COPY . .
RUN pip install --no-cache-dir ./backend \
 && if [ -f converter/pyproject.toml ]; then pip install --no-cache-dir ./converter; fi

VOLUME /data
EXPOSE 8000
CMD ["sh", "-c", "uvicorn floored.api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
