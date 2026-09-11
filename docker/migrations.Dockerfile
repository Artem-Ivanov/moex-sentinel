# syntax=docker/dockerfile:1.7

FROM moex-sentinel-python-base:local

COPY pyproject.toml ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src

RUN python -m pip install --no-cache-dir --no-deps .

USER sentinel

CMD ["moex-migrate-schema"]
