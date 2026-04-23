FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG TYPST_VERSION=0.13.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev docker-cli curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    ARCH=$(dpkg --print-architecture); \
    case "$ARCH" in \
      amd64) TYPST_ARCH="x86_64-unknown-linux-musl" ;; \
      arm64) TYPST_ARCH="aarch64-unknown-linux-musl" ;; \
      *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-${TYPST_ARCH}.tar.xz" \
      | tar -xJ --strip-components=1 -C /usr/local/bin "typst-${TYPST_ARCH}/typst"; \
    typst --version

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate && uvicorn SmartTeX.asgi:application --host 0.0.0.0 --port 8000"]
