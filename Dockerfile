# Chromium has to be in the image: the healer opens a real browser at runtime,
# not just during a build step.
FROM python:3.12-slim AS console
WORKDIR /build
RUN --mount=type=cache,target=/root/.npm \
    apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/* \
    && npm install -g pnpm@11
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./frontend/
RUN cd frontend && pnpm install --frozen-lockfile
COPY frontend ./frontend
RUN cd frontend && pnpm build


FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && uv pip install --system --no-cache .

# system libs + the browser binary
RUN playwright install --with-deps chromium

COPY app ./app
COPY portal ./portal
COPY bench ./bench
COPY --from=console /build/frontend/dist ./frontend/dist

ENV PORT=8080
EXPOSE 8080

# Cloud Run: 2 vCPU / 2GiB minimum — Chromium OOMs under 1GiB.
CMD exec uvicorn app.server:app --host 0.0.0.0 --port ${PORT} --workers 1
