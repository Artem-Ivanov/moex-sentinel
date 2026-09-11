# syntax=docker/dockerfile:1.7

FROM moex-sentinel-python-base:local

COPY pyproject.toml ./
COPY src ./src

RUN python -m pip install --no-cache-dir --no-deps .

USER sentinel

CMD ["portfolio-snapshot-worker"]
