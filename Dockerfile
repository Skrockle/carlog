FROM python:3.12-alpine
WORKDIR /app
COPY server.py /app/server.py
COPY static /app/static
ENV CARLOG_DB=/data/carlog.db \
    CARLOG_PORT=8077 \
    TZ=Europe/Berlin
VOLUME /data
EXPOSE 8077
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:8077/api/health >/dev/null || exit 1
CMD ["python3", "server.py"]
