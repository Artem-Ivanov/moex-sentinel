# syntax=docker/dockerfile:1.7

FROM moex-sentinel-python-base:local

# Only market contracts and analytics code enter this service image.
COPY src/sentinel_contracts ./src/sentinel_contracts
COPY src/market_analytics ./src/market_analytics
ENV PYTHONPATH=/app/src

USER sentinel
EXPOSE 8001
CMD ["python", "-m", "market_analytics"]
