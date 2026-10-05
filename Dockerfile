FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /app

COPY app ./app
COPY tests ./tests
COPY scripts ./scripts

RUN adduser --system --uid 1001 orbit && mkdir -p /data && chown orbit /data
USER orbit

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=5 \
    CMD python -c "import json,os,urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','8080'),timeout=2); \
sys.exit(0 if json.load(r).get('status')=='ok' else 1)"

CMD ["python", "-m", "app.server"]
