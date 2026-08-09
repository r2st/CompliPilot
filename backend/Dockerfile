FROM python:3.11-slim

WORKDIR /app

# psycopg[binary] and pypdf's OCR fallback need no extra system packages, but
# building cryptography from source on a slim image without a C toolchain does
# — build-essential is small and this layer is cached unless requirements
# change, so it costs nothing on every subsequent build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x docker-entrypoint.sh \
    && mkdir -p /app/data/documents

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
