# ── Stage 1: React 프론트엔드 빌드 ───────────────────────────
FROM node:20-alpine AS frontend

WORKDIR /frontend

COPY frontend/package.json ./
RUN npm install

COPY frontend/ ./
RUN npm run build

# ── Stage 2: FastAPI 백엔드 + 정적 파일 서빙 ─────────────────
FROM python:3.12-slim

# 보안: 비루트 사용자
RUN useradd -m -u 1000 appuser

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
# 빌드된 React 정적 파일을 backend/static 으로 복사
COPY --from=frontend /frontend/dist ./static

USER appuser

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/api/health')"

ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8501"]
