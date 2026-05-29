FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

ARG TYPST_VERSION=0.13.1

RUN set -eux; \
    sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
      docker-cli \
      curl \
      git \
      xz-utils \
      fontconfig \
      cabextract \
      xfonts-utils \
      ttf-mscorefonts-installer \
      ca-certificates \
      gnupg; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    fc-cache -f -v; \
    rm -rf /var/lib/apt/lists/*

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

COPY package.json /app/package.json
RUN npm install --no-audit --no-fund

COPY . /app

RUN npm run build

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate && uvicorn SmartTeX.asgi:application --host 0.0.0.0 --port 8000"]