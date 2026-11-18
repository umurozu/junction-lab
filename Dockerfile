# ---- web build -------------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- runtime (center + controller share one image) ------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 UV_SYSTEM_PYTHON=1
RUN pip install uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --system .
COPY configs ./configs
COPY --from=web /web/dist ./web/dist
RUN useradd --system --uid 10001 jl && mkdir -p /data && chown jl /data
USER jl
EXPOSE 8000/tcp 15050/udp 1162/udp 161/udp
CMD ["jl-center", "--host", "0.0.0.0", "--port", "8000"]
