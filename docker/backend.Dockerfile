# syntax=docker/dockerfile:1.7

FROM moex-sentinel-python-base:local

COPY pyproject.toml ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src

RUN python -m pip install --no-cache-dir --no-deps .

USER sentinel

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"]

CMD ["python", "-m", "moex_sentinel.entrypoint"]
