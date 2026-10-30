# AccessGuard images. Two targets:
#   web  - the control plane (Django + gunicorn); no sample app, no lab code.
#   demo - the synthetic sample API plus the platform demo runner (trusts only the demo API).
# Pin the base image by digest before production (docs/launch-checklist.md).
FROM python:3.13-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --create-home --uid 10001 accessguard  && mkdir -p /demo-state /backups && chown accessguard /demo-state /backups
WORKDIR /app

FROM base AS web
COPY web/requirements.txt /app/web/requirements.txt
RUN pip install --require-hashes -r /app/web/requirements.txt \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*
COPY src /app/src
COPY web /app/web
ENV PYTHONPATH=/app/src:/app/web DJANGO_SETTINGS_MODULE=accessguard_web.settings
# collectstatic needs settings; a throwaway key is used only for this build step.
RUN ACCESSGUARD_SECRET_KEY=build-only-not-secret python web/manage.py collectstatic --no-input
USER accessguard
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
CMD ["sh", "-c", "python web/manage.py migrate --no-input && python web/manage.py createcachetable && exec gunicorn accessguard_web.wsgi --chdir web --bind 0.0.0.0:8000 --workers ${WEB_CONCURRENCY:-3} --timeout 60 --access-logfile -"]

FROM base AS demo
COPY src /app/src
COPY sampleapp /app/sampleapp
COPY ops/docker/demo_entrypoint.sh /app/demo_entrypoint.sh
ENV PYTHONPATH=/app/src:/app/sampleapp
USER accessguard
CMD ["sh", "/app/demo_entrypoint.sh"]
