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

CMD ["uvicorn", "netmon_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
