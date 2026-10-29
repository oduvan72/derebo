FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cargo \
        libffi-dev \
        libssl-dev \
        pkg-config \
        rustc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip wheel --wheel-dir=/wheels -r requirements.txt

FROM node:24-alpine AS assets

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY scripts ./scripts
COPY tailwind.config.js ./
COPY templates ./templates
COPY static/tailwind.input.css ./static/tailwind.input.css
RUN npm run build:css

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

WORKDIR /app

COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 libgcc-s1 \
    && python -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /var/lib/apt/lists/* /wheels

COPY app.py bot.py db.py ./
COPY templates ./templates
COPY static ./static
COPY --from=assets /app/static/tailwind.css ./static/tailwind.css
COPY --from=assets /app/static/files ./static/files
RUN mkdir -p /app/instance

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
