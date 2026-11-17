# syntax=docker/dockerfile:1
# Build from the repository root:  docker build -f deploy/Dockerfile -t kaizeval .

FROM node:22-alpine AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# vite writes the bundle to ../src/kaizeval/webui
RUN mkdir -p /build/src/kaizeval/webui && npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KAIZEVAL_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY --from=web /build/src/kaizeval/webui ./src/kaizeval/webui
RUN pip install --no-cache-dir ".[postgres,s3]" \
    && useradd --system --uid 10001 --home-dir /data kaizeval \
    && mkdir -p /data && chown -R kaizeval /data
USER kaizeval
VOLUME ["/data"]
EXPOSE 8000
CMD ["kaizeval", "serve", "--host", "0.0.0.0", "--port", "8000"]
