FROM python:3.11-slim

WORKDIR /app

# Install git (needed to install surveyflow from GitHub)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Accept GitHub token as build arg for private repo access
ARG GITHUB_TOKEN
ENV GITHUB_TOKEN=${GITHUB_TOKEN}

COPY app/requirements.txt .

# Rewrite the git+https line to embed the token if provided
RUN if [ -n "$GITHUB_TOKEN" ]; then \
      sed -i "s|git+https://github.com/|git+https://${GITHUB_TOKEN}@github.com/|g" requirements.txt; \
    fi

RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 8000

CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "--bind", "0.0.0.0:8000", "--timeout", "120", "main:app"]
