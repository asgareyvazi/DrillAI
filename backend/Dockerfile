FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DRILLAI_BLOB_ROOT=/var/lib/drillai/blobs

WORKDIR /app

# Install the optional OCR renderer and local Tesseract runtime into the image. OCR remains disabled
# by default; enabling it never triggers a runtime package or language-data download.
RUN apt-get update && \
    apt-get install --no-install-recommends -y tesseract-ocr tesseract-ocr-eng tesseract-ocr-fas && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1001 drillai && \
    useradd --uid 1001 --gid drillai --create-home --shell /bin/bash drillai

COPY pyproject.toml README.md* alembic.ini ./
COPY alembic ./alembic
COPY src ./src

RUN pip install --upgrade pip && \
    pip install '.[ocr]' && \
    mkdir -p /var/lib/drillai/blobs && \
    chown -R drillai:drillai /app /var/lib/drillai

USER drillai

EXPOSE 8000

CMD ["uvicorn", "drillai.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
