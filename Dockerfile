FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CASELOG_DB=/data/caselog.db \
    PORT=8080

WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.3 gunicorn==22.0.0

COPY app.py .
COPY templates/ templates/
COPY docs/icon-180.png ./icon-180.png

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"

CMD ["gunicorn","--bind","0.0.0.0:8080","--workers","2","--access-logfile","-","app:app"]
