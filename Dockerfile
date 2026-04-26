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

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
