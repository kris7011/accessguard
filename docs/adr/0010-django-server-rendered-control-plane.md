# ADR 0010: Django 6.1, server-rendered, as the control plane

- Status: accepted (V1)
- Date: 2026-10-29

## Problem

V1 needs accounts, organisations with roles, forms with validation, migrations, an admin
console, payments and a small JSON API for runners, built and maintained by one developer.
The engine must stay dependency-free; the web part may use well-maintained packages.

## Options

1. **FastAPI + SQLAlchemy/Alembic + React/TypeScript SPA** (the original suggestion).
2. **Django, server-rendered templates, minimal JavaScript.**
3. **Engine-only stdlib HTTP server.**

## Decision

Option 2: Django 6.1 (the current stable release; built-in Content-Security-Policy support),
server-rendered templates, one small script for live scan progress and copy buttons.

- Authentication, sessions, CSRF, password validation and reset, forms, migrations and the
  admin come from the framework instead of being assembled from parts.
- Forms validate fields by constructing the engine's own domain objects
  (`projects/forms.py`), and the whole configuration through the engine's scan-spec parser
  (`projects/config.py`): one rule set for the UI, the runner and the CLI.
- No inline scripts or styles; CSP `default-src 'self'` without `unsafe-inline` (tested).
- SQLite locally, PostgreSQL in production (`DATABASE_URL`). Type-checked with mypy strict
  and django-stubs.

## Consequences

- No separate frontend build; pages work without JavaScript and are easy to test with the
  Django test client.
- Rich client-side interactions (drag-and-drop policy editors) would need more JavaScript.
- Django 6.1 is supported until about December 2027; plan the upgrade to the 6.2 LTS
  (expected April 2027) as a routine task.

## Revisit when

The configuration UI needs heavy client-side interaction, or a public API for third-party
integrations grows beyond the runner API.
