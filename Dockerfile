FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Europe/Berlin \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mail2lexware ./mail2lexware

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir /data

# Der Entrypoint startet als root, korrigiert die Rechte von /data und läuft dann als PUID:PGID
# (Standard 99:100 = nobody:users wie bei Unraid) weiter.
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'], timeout=3)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "mail2lexware"]
