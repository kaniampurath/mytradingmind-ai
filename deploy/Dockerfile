FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY aegis_trader /app/aegis_trader
COPY scripts /app/scripts
RUN pip install --no-cache-dir -e .

CMD ["aegis-trader"]
