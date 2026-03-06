# Flask VPS Deploy

Reusable Linux VPS deployment toolkit for Flask projects.

## What it does

- installs or updates the base packages needed for deployment
- checks whether `caddy` is installed and upgrades it to the latest package version available from the configured repositories
- installs `uv` if it is missing
- deploys from either the current local project directory or a Git repository URL
- creates a `systemd` service for `gunicorn`
- creates a `Caddy` site config for HTTP-only reverse proxy to support Cloudflare origin mode
- starts or reloads the required services

## Included scripts

- `install.sh`: one-line installer for VPS usage
- `scripts/flask_vps_deploy.py`: interactive deployment wizard
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

After installation, you can run the tool in two ways.

Inside a local Flask project directory:

```bash
sudo flask-vps-deploy
```

From anywhere by giving it a repository URL:

```bash
sudo flask-vps-deploy --repo-url https://github.com/yourname/your-flask-app.git
```

## Interactive flow

The wizard now asks for these values in order:

- Git repository URL, or blank to read source from the current directory
- domain name
- service name, defaulting to the project name
- deploy directory, defaulting to `/srv/www/<service-name>`
- run user, defaulting to `www-data` or `caddy`

If the service already exists, the script reuses its existing internal Gunicorn port.
If the service is new, the script automatically picks a free port between `8100` and `8999`.

## Defaults

- Python environment: `uv`
- App server: `gunicorn`
- Reverse proxy: `caddy`
- Process supervisor: `systemd`
- Default deploy root: `/srv/www/<service-name>`
- Internal app port: auto-selected free port on `127.0.0.1`
- Caddy origin mode: HTTP only, intended for Cloudflare-proxied domains

## Non-interactive examples

Deploy from the current directory as the source, then sync into `/srv/www/<service-name>`:

```bash
sudo flask-vps-deploy \
  --domain example.com \
  --service-name example-app \
  --deploy-dir /srv/www/example-app \
  --yes
```

Deploy directly from GitHub:

```bash
sudo flask-vps-deploy \
  --repo-url https://github.com/yourname/your-flask-app.git \
  --domain example.com \
  --service-name example-app \
  --deploy-dir /srv/www/example-app \
  --yes
```

Override the internal Gunicorn port manually:

```bash
sudo flask-vps-deploy \
  --repo-url https://github.com/yourname/your-flask-app.git \
  --domain example.com \
  --port 8201 \
  --yes
```

## Project requirements

The target Flask project should contain at least:

- `pyproject.toml`
- `app.py`
- `wsgi.py`

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

## Notes

- Existing generated service and Caddy config files are backed up with a `.bak` suffix before overwrite.
- The tool targets Linux systems with `apt-get`, `dnf`, or `yum` and requires `systemd`.
- Existing deployments reuse their current internal port so multiple sites can coexist on the same VPS.
