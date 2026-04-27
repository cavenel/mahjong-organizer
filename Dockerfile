FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/base.txt requirements/prod.txt ./requirements/
RUN pip install --no-cache-dir -r requirements/prod.txt

COPY . .

RUN DJANGO_SETTINGS_MODULE=apps.settings.prod \
    DJANGO_SECRET_KEY=dummy-collectstatic \
    ALLOWED_HOSTS=localhost \
    DB_NAME=x DB_USER=x DB_PASSWORD=x \
    python manage.py collectstatic --noinput

# Socket directory — bind-mounted from the gunicorn_sock volume at runtime.
RUN mkdir -p /run/gunicorn

EXPOSE 8000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "apps.asgi:application"]
