FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DRILLAI_BLOB_ROOT=/var/lib/drillai/blobs

WORKDIR /app

# Install the local Tesseract runtime into the image. OCR remains disabled by default; enabling it
# never triggers a runtime package or language-data download. English follows the distro package;
# Persian is a vendored, SHA-256-pinned official tessdata_fast model so deployment/model identity is
# reproducible instead of silently changing with the apt mirror.
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
RUN apt-get update && \
    apt-get install --no-install-recommends -y tesseract-ocr tesseract-ocr-eng && \
    rm -rf /var/lib/apt/lists/*

COPY tessdata/fas.traineddata /tmp/fas.traineddata
COPY tessdata/LICENSE /usr/share/doc/drillai-tessdata/LICENSE
RUN echo 'db1c0a91208aff00d3cf1ed2c1d23f76419afd5f024688b4f71adc3f2ce4a505  /tmp/fas.traineddata' | sha256sum -c - && \
    install -D -m 0644 /tmp/fas.traineddata /usr/share/tesseract-ocr/5/tessdata/fas.traineddata && \
    rm /tmp/fas.traineddata && \
    tesseract --list-langs 2>&1 | grep -Fxq 'fas'

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
