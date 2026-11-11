# ==========================================
# LOBO AI SALES AGENT - PRODUCTION DOCKERFILE
# ==========================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Instala curl para Healthcheck do Easypanel/Docker
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Instala dependências Python em camada cacheada
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copia código-fonte e configurações de tenants
COPY main.py ./
COPY src ./src
COPY data ./data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Executa com 1 worker se estiver usando fakeredis; em produção com Redis real pode escalar workers
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
