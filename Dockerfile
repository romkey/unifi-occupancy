# syntax=docker/dockerfile:1

################################################################################
# base: shared interpreter + project metadata
################################################################################
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

################################################################################
# dev: runtime + test + lint tooling, for the dev/test/lint compose files
################################################################################
FROM base AS dev

RUN pip install -e ".[test,lint]"

COPY tests ./tests

CMD ["python", "-m", "unifi_occupancy"]

################################################################################
# runtime: production image, no test or lint tooling, unprivileged
################################################################################
FROM base AS runtime

RUN pip install . \
    && rm -rf /root/.cache \
    && useradd --system --create-home --uid 10001 occupancy \
    && mkdir -p /config /var/www/html /var/lib/unifi-occupancy \
    && chown occupancy:occupancy /var/www/html /var/lib/unifi-occupancy

USER occupancy

# The sheets and their history are the only things written outside /tmp. These
# exist and are owned by the runtime user so that a fresh named volume inherits
# that ownership; a *bind* mount keeps the host's, so chown those to uid 10001.
VOLUME ["/var/www/html", "/var/lib/unifi-occupancy"]

# The service holds no listening socket; liveness is "the process is alive and
# the event loop is not wedged".
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import unifi_occupancy" || exit 1

ENTRYPOINT ["python", "-m", "unifi_occupancy"]
