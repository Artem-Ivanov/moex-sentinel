# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/sentinel/.local/bin:${PATH}"

RUN groupadd --gid 10001 sentinel \
    && useradd --uid 10001 --gid sentinel --create-home sentinel \
    && mkdir -p /app/data \
    && chown -R sentinel:sentinel /app

WORKDIR /app

COPY pyproject.toml ./
COPY docker/certs/tbank-root.pem docker/certs/tbank-sub.pem /tmp/tbank-certs/

RUN cat /etc/ssl/certs/ca-certificates.crt /tmp/tbank-certs/tbank-root.pem /tmp/tbank-certs/tbank-sub.pem > /tmp/tbank-ca-bundle.pem \
    && PIP_CERT=/tmp/tbank-ca-bundle.pem python -m pip install --no-cache-dir \
      "t-tech-investments>=1.49.3,<1.50.0" \
      --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple \
    && rm -rf /tmp/tbank-certs /tmp/tbank-ca-bundle.pem \
    && python -c 'import tomllib; values = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]; print("\n".join(value for value in values if not value.startswith("t-tech-investments")))' > /tmp/base-requirements.txt \
    && python -m pip install --no-cache-dir -r /tmp/base-requirements.txt
