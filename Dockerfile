# syntax=docker/dockerfile:1

# ---- Builder: install deps, build wheels, compile front-end assets ----
# Everything in this stage (compiler, libpq headers, the Tailwind CLI) is thrown
# away — only the built wheels and the collected static files cross into runtime.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Build toolchain — only needed to compile any sdist-only wheels. Discarded with
# this stage, so gcc/libpq-dev never reach the runtime image. The Pango/Cairo/
# GDK-PixBuf libs + emoji font are WeasyPrint's runtime deps, used only to render
# the docs PDFs below; they too stay in this discarded stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl ca-certificates \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
    libffi-dev libcairo2 shared-mime-info \
    fonts-dejavu-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Build a wheel for every prod dependency, then install those wheels (needed so
# collectstatic below can run). The /wheels dir is bind-mounted into the runtime
# stage so it can install with no compiler present.
COPY requirements/base.txt requirements/prod.txt requirements/docs.txt ./requirements/
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements/prod.txt \
 && pip install --no-cache-dir --no-index --find-links=/wheels -r requirements/prod.txt
# Build-time-only deps (WeasyPrint + Markdown) for build_docs_pdf. Not wheeled
# into /wheels, so they never get installed into the runtime stage.
RUN pip install --no-cache-dir -r requirements/docs.txt

# Standalone Tailwind CLI (no Node toolchain). Build-time only — kept under /opt
# so it never lands on the runtime PATH.
ARG TAILWIND_VERSION=v3.4.17
RUN ARCH=$(dpkg --print-architecture); \
    if [ "$ARCH" = "amd64" ]; then SUFFIX=linux-x64; \
    elif [ "$ARCH" = "arm64" ]; then SUFFIX=linux-arm64; \
    else echo "Unsupported architecture: $ARCH" >&2 && exit 1; fi && \
    curl -sLo /opt/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-${SUFFIX}" && \
    chmod +x /opt/tailwindcss

COPY . .

# Compile Tailwind CSS from source. Scans mahj/templates/**/*.html for used classes.
RUN /opt/tailwindcss -c tailwind.config.js \
                     -i mahj/static/css/tailwind.src.css \
                     -o mahj/static/css/tailwind.min.css \
                     --minify

# Render the admin-console Markdown guides to PDFs under mahj/static/docs/ so the
# collectstatic below picks them up (served at /static/docs/<name>.pdf).
RUN DJANGO_SETTINGS_MODULE=apps.settings.prod \
    DJANGO_SECRET_KEY=dummy-build-docs \
    ALLOWED_HOSTS=localhost \
    DB_NAME=x DB_USER=x DB_PASSWORD=x \
    python manage.py build_docs_pdf

RUN DJANGO_SETTINGS_MODULE=apps.settings.prod \
    DJANGO_SECRET_KEY=dummy-collectstatic \
    ALLOWED_HOSTS=localhost \
    DB_NAME=x DB_USER=x DB_PASSWORD=x \
    python manage.py collectstatic --noinput


# ---- Runtime: slim image, no compiler / Node / Tailwind binary ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Non-root runtime user. Fixed UID/GID (1000) so the host can chown the
# bind-mounted ./captures dir to a matching owner (`chown -R 1000:1000 captures`).
RUN groupadd --gid 1000 app && \
    useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

# Runtime-only native libs: opencv (libglib2.0-0, libgomp1) and the healthcheck's
# curl-over-socket liveness probe. No gcc / libpq-dev — psycopg2-binary bundles
# its own libpq, and nothing is compiled at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies from the prebuilt wheels. The wheel dir is bind-mounted
# from the builder so it is never committed to a layer (no image bloat).
COPY requirements/base.txt requirements/prod.txt ./requirements/
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links=/wheels -r requirements/prod.txt

# App source + compiled tailwind.min.css + collected staticfiles, from the builder
# (the build context is filtered by .dockerignore in the builder's COPY . .).
COPY --from=builder /app /app

# Runtime-writable mountpoints. Creating + chowning these here means a fresh
# named volume (gunicorn_sock, static_files) inherits app ownership on first use;
# /app/mahj/captures covers the dev run where ./captures isn't bind-mounted.
# (Volumes created by an earlier root-only image must be chowned or recreated.)
RUN mkdir -p /run/gunicorn /static /app/mahj/captures && \
    chown -R app:app /app /run/gunicorn /static

USER app

EXPOSE 8000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "apps.asgi:application"]
