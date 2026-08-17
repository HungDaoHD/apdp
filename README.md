# SurveyFlow

A web-based survey data processing app that fetches survey data from **QMe**, processes it through a pipeline, and generates Excel data tables with cross-tabulation and significance testing.

---

## Tech Stack

- **Backend**: FastAPI + Gunicorn + Uvicorn
- **Data processing**: [surveyflow](https://pypi.org/project/surveyflow/) + Pandas + OpenPyXL
- **Frontend**: Static HTML/JS (no framework)
- **Auth**: QMe OAuth 2.0 (MCP)
- **Storage**: Local filesystem or AWS S3

---

## Project Structure

```
apdp/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Settings (reads from .env)
│   ├── requirements.txt
│   ├── routers/
│   │   ├── surveys.py       # Survey search & fetch endpoints
│   │   ├── pipeline.py      # Ingest & table generation endpoints
│   │   └── qme_auth.py      # OAuth login endpoints
│   ├── services/
│   │   ├── mcp_client.py    # QMe MCP client (token management)
│   │   ├── pipeline_svc.py  # surveyflow pipeline wrapper
│   │   └── storage.py       # LocalStorage / S3Storage abstraction
│   └── static/
│       ├── projects.html    # Survey list UI
│       └── editor.html      # Datatable editor UI
└── README.md
```

---

## Local Development

```bash
cd app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in your values
python main.py         # runs on http://localhost:8000
```

---

## Environment Variables

Create `app/.env`:

```ini
# QMe OAuth
QME_CLIENT_ID=your_client_id
QME_CLIENT_SECRET=your_client_secret
QME_REDIRECT_URI=http://localhost:8000/api/qme/callback

# Server
PORT=8000

# Storage
DATA_DIR=data
STORAGE_BACKEND=local   # or "s3"

# AWS S3 (only if STORAGE_BACKEND=s3)
AWS_S3_BUCKET=your-bucket
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=

# MongoDB (workload module — projects/tasks)
MONGODB_URI=mongodb://apdp_app:changeme@127.0.0.1:27018/ap_workload?authSource=ap_workload
MONGODB_DB=ap_workload
```

> MongoDB is self-hosted via the `mongo` service in `docker-compose.yml`,
> not a managed cloud cluster — its data lives in the `mongo_data` named
> volume, so it survives `docker compose down` (only `down -v` wipes it).
> Credentials come from a **root-level** `.env` (next to `docker-compose.yml`,
> separate from `app/.env`), read for `${...}` substitution:
>
> ```ini
> MONGO_ROOT_USER=apdp_root
> MONGO_ROOT_PASSWORD=<strong random value>
> MONGO_APP_USER=apdp_app
> MONGO_APP_PASSWORD=<strong random value>
> MONGO_INITDB_DATABASE=ap_workload
> ```
>
> `mongo-init.js` runs once (only on an empty data volume) to create the
> `apdp_app` user scoped to `readWrite` on `ap_workload` only — the app never
> authenticates as `apdp_root`, so a compromised app process can't touch any
> other database or run admin commands. When running via `docker compose up`,
> the `app` service's `MONGODB_URI` is set from those same variables
> automatically (overriding whatever is in `app/.env`) and points at the
> `mongo` service by its container name, not localhost.
>
> The container's port is published as `127.0.0.1:27018 -> 27017` — reachable
> from this host only (for `mongosh`, local test scripts run outside Docker),
> never from the LAN or internet. 27018 rather than the default 27017 avoids
> colliding with a `mongod` already installed directly on the host, if there
> is one. `app/.env`'s own `MONGODB_URI` (shown above) is what's used for that
> host-side path — running `python main.py` directly, without Docker.
>
> To change these credentials later: stop the stack, delete the `mongo_data`
> volume (`docker compose down -v`), update the root `.env`, and start again
> — `mongo-init.js` only runs against an empty volume, so it won't pick up
> new values on an existing one.

---

## Ubuntu Server Deployment

### 1. Install dependencies

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv nginx
```

### 2. Clone and install

```bash
cd /opt
sudo git clone <repo-url> surveyflow
sudo chown -R $USER:$USER /opt/surveyflow

cd /opt/surveyflow/app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
nano /opt/surveyflow/app/.env   # fill in all required values
```

### 4. Systemd service

Create `/etc/systemd/system/surveyflow.service`:

```ini
[Unit]
Description=SurveyFlow FastAPI App
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/surveyflow/app
EnvironmentFile=/opt/surveyflow/app/.env
ExecStart=/opt/surveyflow/app/.venv/bin/gunicorn \
    -k uvicorn.workers.UvicornWorker \
    -w 1 \
    --bind 127.0.0.1:8000 \
    --timeout 120 \
    main:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable surveyflow
sudo systemctl start surveyflow
```

### 5. Nginx reverse proxy

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 50M;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/surveyflow /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 6. HTTPS (optional)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

> After enabling HTTPS, update `QME_REDIRECT_URI` in `.env` to use `https://` and restart the service.

### Useful commands

| Task | Command |
|---|---|
| Restart app | `sudo systemctl restart surveyflow` |
| View logs | `sudo journalctl -u surveyflow -f` |
| Pull updates | `git pull && sudo systemctl restart surveyflow` |
| Nginx error logs | `sudo tail -f /var/log/nginx/error.log` |

---

## Changing the Port

Update `PORT` in `app/.env`:

```ini
PORT=9000
```

Also update the `--bind` flag in the systemd service and `QME_REDIRECT_URI` to match.
