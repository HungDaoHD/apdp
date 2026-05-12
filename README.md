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
```

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
