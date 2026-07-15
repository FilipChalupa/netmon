FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY netmon_server ./netmon_server

ENV NETMON_DB=/data/netmon.db \
    NETMON_MONITORS=/data/monitors.toml \
    NETMON_TZ=Europe/Prague

VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "netmon_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
