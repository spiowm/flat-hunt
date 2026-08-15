FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Kyiv \
    FLAT_HUNT_DB=/data/flats.db

WORKDIR /app

RUN pip install --no-cache-dir requests==2.32.3 PyYAML==6.0.2 curl_cffi==0.7.4

COPY flathunt/ ./flathunt/
COPY run.py config.yaml ./

RUN mkdir -p /data
VOLUME ["/data"]

# Перевірка живості: база доступна і збирач хоч раз відпрацював
HEALTHCHECK --interval=5m --timeout=20s --start-period=2m \
  CMD python3 -c "import sqlite3,os,sys; \
      c=sqlite3.connect(os.environ['FLAT_HUNT_DB']); \
      sys.exit(0 if c.execute('select count(*) from runs').fetchone()[0] else 1)"

CMD ["python3", "run.py", "watch", "--interval", "600"]
