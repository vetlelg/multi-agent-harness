# One engine, every domain. The DOMAIN setting picks the domain at startup; without
# it the engine refuses to start.
#
#   docker build -f infra/base/docker/orchestrator.Dockerfile -t harness/orchestrator-scratch:dev .
#   (LangGraph: --build-arg ENGINE=orchestrator_lg --build-arg REQUIREMENTS=requirements-lg.txt)
#
# Everything up to and including useradd is identical to service.Dockerfile, so the
# dependency layer is built once and shared.
FROM python:3.12.15-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

ARG REQUIREMENTS=requirements.txt
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r ${REQUIREMENTS}
RUN useradd --system --uid 10001 app

ARG ENGINE=orchestrator_scratch
ENV ENGINE=${ENGINE}

COPY core/ core/
COPY domains/ domains/
COPY ${ENGINE}/ ${ENGINE}/

# Numeric, so Kubernetes' runAsNonRoot can verify it.
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)"]
# exec: uvicorn replaces the shell as PID 1, so it receives SIGTERM.
CMD ["sh", "-c", "exec uvicorn \"${ENGINE}.app:app\" --host 0.0.0.0 --port 8000"]
