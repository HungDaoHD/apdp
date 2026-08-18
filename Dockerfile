FROM python:3.11-slim

WORKDIR /app

# git: needed to `pip install` the surveyflow dependency from GitHub.
# (privilege drop in docker-entrypoint.sh uses `setpriv` from util-linux,
# which ships with the base image already — no extra package needed; `gosu`
# is deliberately NOT used here since it isn't an apt package on Debian, it
# requires downloading+GPG-verifying a binary from GitHub at build time)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY version.txt .
COPY app/ .

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "--bind", "0.0.0.0:8000", "--timeout", "300", "--graceful-timeout", "30", "main:app"]
