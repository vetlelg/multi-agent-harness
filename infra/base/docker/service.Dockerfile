# One agent (or plain tool server): the harness plus exactly one domain, serving one app.
#
#   docker build -f infra/base/docker/service.Dockerfile \
#     --build-arg DOMAIN=<name> --build-arg APP=<module>:app -t harness/<name>-<service>:dev .
#
# Everything up to and including useradd is identical to orchestrator.Dockerfile, so
# the dependency layer is built once and shared. Keep it that way: an ARG declared
# earlier becomes part of the cache key of every RUN after it.
FROM python:3.12.15-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

ARG REQUIREMENTS=requirements.txt
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r ${REQUIREMENTS}
RUN useradd --system --uid 10001 app

ARG DOMAIN
ARG APP
# An empty DOMAIN would turn the COPY below into domains// -- every domain, silently.
RUN test -n "$DOMAIN" && test -n "$APP" \
    || { echo "build args DOMAIN and APP are required" >&2; exit 1; }
ENV APP=${APP}

COPY core/ core/
COPY domains/__init__.py domains/
COPY domains/${DOMAIN}/ domains/${DOMAIN}/

# Numeric, so Kubernetes' runAsNonRoot can verify it.
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)"]
# exec: uvicorn replaces the shell as PID 1, so it receives SIGTERM.
CMD ["sh", "-c", "exec uvicorn \"$APP\" --host 0.0.0.0 --port 8000"]
