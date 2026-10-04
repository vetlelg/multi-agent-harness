# The sql executor: the harness, the sql domain, and the Chinook database baked in.
#
#   make db
#   docker build -f domains/sql/tools/executor/Dockerfile -t harness/sql-executor:dev .
#
# Its own Dockerfile rather than the generic one: this is the only image that may
# contain the database, so the executor stays the only service with data access.
FROM python:3.12.15-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements-tool.txt ./
RUN pip install --no-cache-dir -r requirements-tool.txt
RUN useradd --system --uid 10001 app

COPY core/ core/
COPY domains/__init__.py domains/
COPY domains/sql/ domains/sql/
# At the default SQL_DB_PATH, relative to /app.
COPY data/chinook.db data/chinook.db

# Numeric, so Kubernetes' runAsNonRoot can verify it.
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)"]
CMD ["uvicorn", "domains.sql.tools.executor.app:app", "--host", "0.0.0.0", "--port", "8000"]
