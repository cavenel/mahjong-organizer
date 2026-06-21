FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Non-root runtime user. Fixed UID/GID (1000) so the host can chown the
# bind-mounted ./captures dir to a matching owner (`chown -R 1000:1000 captures`).
RUN groupadd --gid 1000 app && \
    useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl ca-certificates \
    libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Standalone Tailwind CLI (no Node toolchain required).
ARG TAILWIND_VERSION=v3.4.17
RUN ARCH=$(dpkg --print-architecture); \
    if [ "$ARCH" = "amd64" ]; then SUFFIX=linux-x64; \
    elif [ "$ARCH" = "arm64" ]; then SUFFIX=linux-arm64; \
    else echo "Unsupported architecture: $ARCH" >&2 && exit 1; fi && \
    curl -sLo /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-${SUFFIX}" && \
    chmod +x /usr/local/bin/tailwindcss

COPY requirements/base.txt requirements/prod.txt ./requirements/
RUN pip install --no-cache-dir -r requirements/prod.txt

COPY . .

# Compile Tailwind CSS from source. Scans mahj/templates/**/*.html for used classes.
RUN tailwindcss -c tailwind.config.js \
                -i mahj/static/css/tailwind.src.css \
                -o mahj/static/css/tailwind.min.css \
                --minify

RUN DJANGO_SETTINGS_MODULE=apps.settings.prod \
    DJANGO_SECRET_KEY=dummy-collectstatic \
    ALLOWED_HOSTS=localhost \
    DB_NAME=x DB_USER=x DB_PASSWORD=x \
    python manage.py collectstatic --noinput

# Runtime-writable mountpoints. Creating + chowning these here means a fresh
# named volume (gunicorn_sock, static_files) inherits app ownership on first use;
# /app/mahj/captures covers the dev run where ./captures isn't bind-mounted.
# (Volumes created by an earlier root-only image must be chowned or recreated.)
RUN mkdir -p /run/gunicorn /static /app/mahj/captures && \
    chown -R app:app /app /run/gunicorn /static

USER app

EXPOSE 8000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "apps.asgi:application"]
