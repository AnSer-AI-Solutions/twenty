FROM python:3.13-alpine@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d

RUN pip install --no-cache-dir "psycopg[binary]==3.2.9" \
    && addgroup -S -g 10001 exporter \
    && adduser -S -D -H -u 10001 -G exporter exporter

WORKDIR /app
COPY crm_metrics_exporter.py /app/crm_metrics_exporter.py

USER 10001:10001
EXPOSE 9105
ENTRYPOINT ["python", "/app/crm_metrics_exporter.py"]
