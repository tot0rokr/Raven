# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS novnc
ADD --checksum=sha256:b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6 https://github.com/novnc/noVNC/archive/refs/tags/v1.7.0.tar.gz /tmp/novnc.tar.gz
RUN tar -xzf /tmp/novnc.tar.gz -C /tmp

FROM python:3.12-slim-bookworm AS backend
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RAVEN_WEB_STATE=/data
WORKDIR /app
COPY web/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY web /app
COPY --from=novnc /tmp/noVNC-1.7.0/core /app/static/novnc/core
COPY --from=novnc /tmp/noVNC-1.7.0/vendor /app/static/novnc/vendor
COPY --from=novnc /tmp/noVNC-1.7.0/LICENSE.txt /app/static/novnc/LICENSE.txt

FROM backend AS test
RUN pip install --no-cache-dir --require-hashes -r requirements-test.txt
CMD ["python", "manage.py", "test", "tests", "--settings=raven_web.test_settings", "--verbosity=2"]

FROM backend AS runtime
USER 60001:60001
EXPOSE 8443
CMD ["python", "-m", "uvicorn", "raven_web.asgi:application", "--host", "0.0.0.0", "--port", "8443", "--workers", "1", "--ssl-certfile", "/data/tls.crt", "--ssl-keyfile", "/data/tls.key", "--ws-max-size", "1048576", "--no-proxy-headers", "--no-access-log", "--no-server-header"]
