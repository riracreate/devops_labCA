# syntax=docker/dockerfile:1
#
# One Dockerfile, two images:
#   runtime (default)  production image: app code only, runs as a non-root user
#   test               adds pytest/ruff and both test suites; CI uses it for the
#                      unit tests and as the containerised resilience test runner
#
#   docker build -t catalog-api .                        # runtime
#   docker build --target test -t catalog-api-tests .    # test runner

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Dependencies change rarely, so they get their own cached layer.
# Built to survive slow or flaky networks: pip waits up to 120 s per read and
# retries 10 times (defaults: 15 s, 5 retries), and downloads are kept in a
# BuildKit cache outside the image, so a failed build resumes where it stopped.
FROM base AS deps
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 -r requirements.txt

FROM deps AS test
COPY requirements-dev.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 -r requirements-dev.txt
COPY pyproject.toml .
COPY catalog ./catalog
COPY tests ./tests
# Default command: unit tests. CI overrides it to run tests/resilience.
CMD ["pytest", "-p", "no:cacheprovider"]

FROM deps AS runtime
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser
COPY catalog ./catalog
# Numeric so orchestrators (e.g. Kubernetes runAsNonRoot) can verify it.
USER 10001:10001
EXPOSE 8000
# Liveness only: "healthy" means the process answers HTTP. Whether PostgreSQL is
# reachable is reported separately on /health/ready, so a database outage never
# makes the container itself look broken.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
CMD ["uvicorn", "catalog.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
