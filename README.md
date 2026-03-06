# Flask VPS Deploy

Reusable Linux VPS deployment toolkit for Flask projects.

## What it does

- installs or updates the base packages needed for deployment
- checks whether `caddy` is installed and, on Debian or Ubuntu, installs it from the official Caddy apt repository before upgrading
- installs `uv` if it is missing
- deploys from either the current local project directory or a Git repository URL
- restarts an existing app service after redeploy so new code is actually loaded
- runs post-deploy health checks and rolls back to the previous version if checks fail
- creates a `systemd` service for `gunicorn`
- creates a `Caddy` site config for HTTP-only reverse proxy to support Cloudflare origin mode
- provides built-in `status`, `logs`, `access-logs`, `list`, and `self-update` commands

## Included scripts

- `install.sh`: one-line installer for VPS usage
- `scripts/flask_vps_deploy.py`: interactive deployment and maintenance CLI
- `scripts/install_flask_vps_deploy.sh`: local installer used by `install.sh`

## Install on a VPS

Recommended one-line install:

```bash
curl -fsSL https://raw.githubusercontent.com/adampei/flask-vps-deploy/main/install.sh | sudo bash
```

This installer downloads the required files into a temporary directory, installs the CLI into `/usr/local/bin/flask-vps-deploy`, and removes the temporary files automatically.

If you prefer cloning the repository first:

```bash
git clone https://github.com/adampei/flask-vps-deploy.git
cd flask-vps-deploy
sudo bash scripts/install_flask_vps_deploy.sh
```

## Main commands

Deploy or update a site:

```bash
sudo flask-vps-deploy
```

Show one service status, or all managed sites if omitted:

```bash
flask-vps-deploy status
flask-vps-deploy status anime-tactical-simulator-site
```

Show app journald logs:

```bash
flask-vps-deploy logs anime-tactical-simulator-site
flask-vps-deploy logs anime-tactical-simulator-site -f
```

Show Caddy access logs:

```bash
flask-vps-deploy access-logs anime-tactical-simulator-site
flask-vps-deploy access-logs anime-tactical-simulator-site -f
```

List all managed sites:

```bash
flask-vps-deploy list
```

Update the deployment tool itself:

```bash
sudo flask-vps-deploy self-update
```

## Interactive deploy flow

The deploy wizard asks for these values in order:

- Git repository URL, or blank to use the current directory as the source
- deploy directory, defaulting to `/srv/www/<project-name>`
- domain name
- service name, defaulting to the project name
- run user, defaulting to `www-data` or `caddy`

If the service already exists, the script reuses its existing internal Gunicorn port.
If the service is new, the script automatically picks a free port between `8100` and `8999`.
If you leave the repository URL blank, the current directory must already be the Flask project source directory.

## Deploy behavior

Repository mode:

- existing checkout: `git fetch --all --prune` + `git pull --ff-only`
- new checkout: `git clone`

Local source mode:

- syncs the current source directory into the deploy directory with `rsync`

Redeploy behavior:

- an existing service is explicitly restarted after files and dependencies are updated
- post-deploy health checks probe both Gunicorn and Caddy locally
- if health checks fail, the tool restores the previous deploy directory and config files when backups exist

## Defaults

- Python environment: `uv`
- App server: `gunicorn`
- Reverse proxy: `caddy`
- Process supervisor: `systemd`
- Default deploy root: `/srv/www/<service-name>`
- Internal app port: auto-selected free port on `127.0.0.1`
- Gunicorn workers: `2`
- Gunicorn timeout: `60`
- WSGI module: `wsgi:app`
- Health check path: `/`
- Caddy origin mode: HTTP only, intended for Cloudflare-proxied domains

## Common deploy examples

Deploy directly from GitHub:

```bash
sudo flask-vps-deploy \
  --repo-url https://github.com/yourname/your-flask-app.git \
  --domain example.com
```

Deploy from the current directory as the source, then sync into `/srv/www/<service-name>`:

```bash
sudo flask-vps-deploy \
  --domain example.com \
  --service-name example-app \
  --deploy-dir /srv/www/example-app \
  --yes
```

Override Gunicorn settings and health path:

```bash
sudo flask-vps-deploy \
  --repo-url https://github.com/yourname/your-flask-app.git \
  --domain example.com \
  --workers 4 \
  --timeout 120 \
  --wsgi-module app:app \
  --health-path /healthz \
  --yes
```

## Project requirements

The target Flask project should contain at least:

- `pyproject.toml`
- `app.py`
- `wsgi.py`

If you use a different entrypoint, pass it with `--wsgi-module`.

## Git behavior

When `--repo-url` is used, the tool will:

- ensure global `git user.name` and `git user.email` exist
- check whether the repository is accessible
- prompt for GitHub username and token/password only if HTTPS access fails
- store GitHub credentials with `git credential.helper store` when you choose to provide them

This credential helper stores secrets on disk for the root user in plain text. Use a GitHub token with the minimum required scope.

## Caddy behavior

The generated Caddy site config listens on:

- `http://example.com`
- `http://www.example.com`

That keeps the origin side HTTP-only so Cloudflare can terminate HTTPS at the edge while Caddy reverse proxies to Gunicorn locally.

## Logging model

- application and Gunicorn stdout or stderr logs go into `systemd` and are read with `flask-vps-deploy logs <service>`
- Caddy access logs are written per site to `/var/log/caddy/<service>.access.log` and are read with `flask-vps-deploy access-logs <service>`
- because journald stores logs by unit, multiple apps do not mix when you query one specific service with `journalctl -u <service>.service`

## Notes

- Existing generated service and Caddy config files are backed up with a `.bak` suffix before overwrite.
- Existing deploy directories are backed up under `/var/lib/flask-vps-deploy/backups/<service-name>/` before update.
- The tool targets Linux systems with `apt-get`, `dnf`, or `yum` and requires `systemd`.
- On Debian or Ubuntu, the script configures the official Caddy apt repository from Caddy before installing or upgrading `caddy`.
- Existing deployments reuse their current internal port so multiple sites can coexist on the same VPS.
