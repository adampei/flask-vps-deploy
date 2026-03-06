# Flask VPS Deploy

Reusable Linux VPS deployment toolkit for Flask projects.

## What it does

- installs base packages needed for deployment
- installs `uv` if it is missing
- syncs the current Flask project into a target deploy directory
- creates a `systemd` service for `gunicorn`
- creates a `Caddy` site config for reverse proxy and HTTPS
- starts or reloads the required services

## Included scripts

- `scripts/flask_vps_deploy.py`: interactive deployment wizard
- `scripts/install_flask_vps_deploy.sh`: installs the CLI into `/usr/local/bin/flask-vps-deploy`

## Install on a VPS

```bash
sudo bash scripts/install_flask_vps_deploy.sh
```

Then run it inside a Flask project directory:

```bash
sudo flask-vps-deploy
```

## Defaults

- Python environment: `uv`
- App server: `gunicorn`
- Reverse proxy: `caddy`
- Process supervisor: `systemd`
- Internal app port: `127.0.0.1:8008`

## Non-interactive example

```bash
sudo flask-vps-deploy --domain example.com --deploy-dir /var/www/example-com --yes
```

## Requirements

The target Flask project directory should contain:

- `pyproject.toml`
- `app.py`
- `wsgi.py`

## Notes

- Existing generated service and Caddy config files are backed up with a `.bak` suffix before overwrite.
- The tool currently targets Linux systems with `apt-get`, `dnf`, or `yum` and requires `systemd`.
