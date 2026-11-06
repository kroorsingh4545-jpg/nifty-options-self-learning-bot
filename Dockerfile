# NIFTY options self-learning bot - PAPER trading image.
#
# This build has NO LIVE order routing: TRADING_MODE is pinned to PAPER here
# and in docker-compose.yml, and TRADING_MODE=LIVE is refused at config load
# anyway. Real Upstox market data in, simulated (PAPER) orders out.
#
#   docker compose build
#   docker compose run --rm bot python -m runtime check
#   docker compose up -d
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata \
    TRADING_MODE=PAPER \
    DATABASE_PATH=/app/var/trading.db

# libgomp1: LightGBM's OpenMP runtime. tzdata: IST wall clock for logs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only PyTorch first (large, rarely changes: its own cached layer).
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.3"

COPY pyproject.toml README.md ./
COPY backtest ./backtest
COPY broker ./broker
COPY config ./config
COPY core ./core
COPY dashboard ./dashboard
COPY data ./data
COPY execution ./execution
COPY learning ./learning
COPY ml ./ml
COPY monitoring ./monitoring
COPY options ./options
COPY risk ./risk
COPY runtime ./runtime
COPY strategies ./strategies
COPY .streamlit ./.streamlit

# Editable install: config/, the .env lookup and relative paths resolve
# against /app (the project root), exactly as in a source checkout.
RUN pip install -e . \
    && useradd --create-home --uid 10001 bot \
    && mkdir -p /app/var /app/logs /app/reports /app/models /app/data/dev \
    && chown -R bot:bot /app/var /app/logs /app/reports /app/models /app/data/dev

USER bot

# Database, research runs, logs, reports and models live on volumes.
VOLUME ["/app/var", "/app/data/dev", "/app/logs", "/app/reports", "/app/models"]

EXPOSE 8501

# The bot publishes a heartbeat every second; unhealthy if it stops.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "runtime", "health"]

CMD ["python", "-m", "runtime", "paper"]
