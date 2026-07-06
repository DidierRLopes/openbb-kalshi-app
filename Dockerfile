FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    KALSHI_CACHE_DIR=/cache

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /cache
VOLUME ["/cache"]

EXPOSE 7779

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-7779}"]
