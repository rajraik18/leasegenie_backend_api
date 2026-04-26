FROM python:3.12-slim

# System deps:
#   - tesseract-ocr        OCR fallback for scanned PDFs (PaddleOCR is preferred when installed)
#   - curl                 healthchecks
#   - libpq5               psycopg2 runtime (psycopg2-binary already bundles libpq, but the
#                          system lib avoids issues with older glibc images)
#   - build-essential      kept for any source-built optional deps (paddleocr, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
        build-essential libpq5 \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Drop privileges — the app does not need root at runtime. uploads/ and
# exports/ are created on import via settings.ensure_dirs() so they need
# to be writable by this UID.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/uploads /app/exports \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
