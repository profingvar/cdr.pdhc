FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9046

CMD ["gunicorn", "--bind", "0.0.0.0:9046", "--workers", "2", "--timeout", "120", "wsgi:app"]
