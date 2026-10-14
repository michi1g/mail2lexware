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

COPY workspace2lexware ./workspace2lexware

# 99:100 = nobody:users, die Standardbenutzer von Unraid
RUN mkdir /data && chown 99:100 /data
USER 99:100
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'], timeout=3)"

CMD ["python", "-m", "workspace2lexware"]
