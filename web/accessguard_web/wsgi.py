"""WSGI entry point (gunicorn accessguard_web.wsgi)."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "accessguard_web.settings")
application = get_wsgi_application()
