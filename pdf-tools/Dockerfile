FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-writer \
        fonts-dejavu-core \
        fonts-noto-cjk \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p generated uploads_tmp temp

EXPOSE 10000

CMD gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 180 app:app
