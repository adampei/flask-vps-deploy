#!/usr/bin/env python3
from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path


DEFAULT_PORT = 8008
RSYNC_EXCLUDES = [
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
]


def print_step(message: str) -> None:
    print(f"\n==> {message}")


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    pretty = " ".join(shlex.quote(part) for part in cmd)
    print(f"$ {pretty}")
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def run_shell(cmd: str) -> None:
    print(f"$ {cmd}")
    subprocess.run(cmd, shell=True, check=True)


def require_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("This command must run as root. Use sudo or switch to root.")


def detect_package_manager() -> str:
    for binary in ("apt-get", "dnf", "yum"):
        if shutil.which(binary):
            return binary
    raise SystemExit("Unsupported Linux distribution. Only apt-get, dnf, and yum are supported.")


def detect_default_run_user() -> str:
    for candidate in ("www-data", "caddy"):
        try:
            pwd.getpwnam(candidate)
            return candidate
        except KeyError:
            continue
    return "root"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "site"


def prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{question}{suffix}: ").strip()
    return value or (default or "")


def prompt_yes_no(question: str, default: bool = True) -> bool:
    label = "Y/n" if default else "y/N"
    value = input(f"{question} [{label}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def validate_domain(domain: str) -> str:
    domain = domain.strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        raise SystemExit(f"Invalid domain: {domain}")
    return domain


def resolve_source_dir(value: str | None) -> Path:
    source_dir = Path(value or os.getcwd()).expanduser().resolve()
    required = ["pyproject.toml", "app.py", "wsgi.py"]
    missing = [name for name in required if not (source_dir / name).exists()]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Source directory is missing required files: {joined}")
    return source_dir


def ensure_user_and_group(user: str) -> tuple[str, str]:
    try:
        user_entry = pwd.getpwnam(user)
    except KeyError as exc:
        raise SystemExit(f"System user does not exist: {user}") from exc
    group = grp.getgrgid(user_entry.pw_gid).gr_name
    return user, group


def install_base_packages(package_manager: str) -> None:
    print_step("Installing base packages")
    if package_manager == "apt-get":
        run(["apt-get", "update"])
        run(
            [
                "apt-get",
                "install",
                "-y",
                "python3",
                "python3-venv",
                "curl",
                "rsync",
                "ca-certificates",
                "caddy",
            ]
        )
        return

    run(
        [
            package_manager,
            "install",
            "-y",
            "python3",
            "curl",
            "rsync",
            "ca-certificates",
            "caddy",
        ]
    )


def install_uv_if_needed() -> None:
    if shutil.which("uv"):
        print_step("uv already installed")
        run(["uv", "--version"])
        return

    print_step("Installing uv")
    run_shell("curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh")
    run(["uv", "--version"])


def ensure_not_nested(source_dir: Path, deploy_dir: Path) -> None:
    try:
        deploy_dir.relative_to(source_dir)
    except ValueError:
        return
    if deploy_dir != source_dir:
        raise SystemExit("Deploy directory must not be nested inside the source directory.")


def sync_project(source_dir: Path, deploy_dir: Path, user: str, group: str) -> None:
    print_step("Syncing project files")
    deploy_dir.mkdir(parents=True, exist_ok=True)
    if source_dir == deploy_dir:
        print("Source directory matches deploy directory, skipping rsync.")
    else:
        cmd = ["rsync", "-a", "--delete"]
        for pattern in RSYNC_EXCLUDES:
            cmd.extend(["--exclude", pattern])
        cmd.extend([f"{source_dir}/", f"{deploy_dir}/"])
        run(cmd)

    run(["chown", "-R", f"{user}:{group}", str(deploy_dir)])


def sync_python_env(deploy_dir: Path) -> None:
    print_step("Syncing Python environment with uv")
    lock_file = deploy_dir / "uv.lock"
    cmd = ["uv", "sync", "--frozen"] if lock_file.exists() else ["uv", "sync"]
    run(cmd, cwd=deploy_dir)


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_text()
        if current == content:
            return
        backup = path.with_name(f"{path.name}.bak")
        shutil.copy2(path, backup)
    path.write_text(content)


def ensure_caddy_import(main_caddyfile: Path) -> None:
    import_line = "import /etc/caddy/sites-enabled/*"
    if main_caddyfile.exists():
        content = main_caddyfile.read_text()
        if import_line in content:
            return
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n{import_line}\n"
        main_caddyfile.write_text(content)
        return

    main_caddyfile.write_text(
        textwrap.dedent(
            f"""\
            {{
                auto_https on
            }}

            {import_line}
            """
        )
    )


def render_service(service_name: str, deploy_dir: Path, run_user: str, run_group: str, port: int) -> str:
    return textwrap.dedent(
        f"""\
        [Unit]
        Description={service_name} Flask app
        After=network.target

        [Service]
        Type=simple
        User={run_user}
        Group={run_group}
        WorkingDirectory={deploy_dir}
        Environment=PYTHONUNBUFFERED=1
        ExecStart={deploy_dir}/.venv/bin/gunicorn --workers 3 --bind 127.0.0.1:{port} --timeout 60 wsgi:app
        Restart=always
        RestartSec=5

        [Install]
        WantedBy=multi-user.target
        """
    )


def render_caddy(domain: str, port: int, log_name: str) -> str:
    site_labels = [domain]
    if not domain.startswith("www."):
        site_labels.append(f"www.{domain}")

    labels = ", ".join(site_labels)
    return textwrap.dedent(
        f"""\
        {labels} {{
            encode zstd gzip

            header {{
                Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
                X-Content-Type-Options "nosniff"
                X-Frame-Options "SAMEORIGIN"
                Referrer-Policy "strict-origin-when-cross-origin"
            }}

            reverse_proxy 127.0.0.1:{port}

            log {{
                output file /var/log/caddy/{log_name}.access.log
                format console
            }}
        }}
        """
    )


def apply_systemd(service_name: str) -> None:
    print_step("Reloading systemd and starting app service")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", service_name])
    run(["systemctl", "is-active", service_name])


def apply_caddy() -> None:
    print_step("Validating and reloading Caddy")
    run(["caddy", "fmt", "--overwrite", "/etc/caddy/Caddyfile"])
    sites_enabled = Path("/etc/caddy/sites-enabled")
    for path in sorted(sites_enabled.glob("*.conf")):
        run(["caddy", "fmt", "--overwrite", str(path)])
    run(["caddy", "validate", "--config", "/etc/caddy/Caddyfile"])
    run(["systemctl", "enable", "--now", "caddy"])
    run(["systemctl", "reload", "caddy"])
    run(["systemctl", "is-active", "caddy"])


def print_summary(domain: str, deploy_dir: Path, service_name: str, port: int, run_user: str, source_dir: Path) -> None:
    print("\nDeployment summary")
    print("------------------")
    print(f"Source dir : {source_dir}")
    print(f"Deploy dir : {deploy_dir}")
    print(f"Domain     : {domain}")
    print(f"Service    : {service_name}")
    print(f"Run user   : {run_user}")
    print(f"App port   : {port}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interactive VPS deploy wizard for Flask projects using uv, gunicorn, caddy, and systemd."
    )
    parser.add_argument("--source-dir", help="Project source directory. Defaults to the current directory.")
    parser.add_argument("--domain", help="Primary domain for Caddy.")
    parser.add_argument("--deploy-dir", help="Target deploy directory on the VPS.")
    parser.add_argument("--service-name", help="systemd service name.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Internal Gunicorn port. Default: {DEFAULT_PORT}")
    parser.add_argument("--run-user", help="Linux user for the app service. Defaults to www-data or caddy.")
    parser.add_argument("--yes", action="store_true", help="Accept defaults without confirmation.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    require_root()
    if not shutil.which("systemctl"):
        raise SystemExit("systemd is required on the target VPS.")

    source_dir = resolve_source_dir(args.source_dir)

    domain = validate_domain(args.domain) if args.domain else validate_domain(prompt("Domain"))
    service_name = args.service_name or slugify(domain)
    deploy_default = f"/var/www/{service_name}"
    deploy_dir = Path(args.deploy_dir or prompt("Deploy directory", deploy_default)).expanduser().resolve()
    ensure_not_nested(source_dir, deploy_dir)

    package_manager = detect_package_manager()
    install_base_packages(package_manager)
    install_uv_if_needed()

    run_user_input = args.run_user or prompt("Run user", detect_default_run_user())
    run_user, run_group = ensure_user_and_group(run_user_input)
    port = args.port

    print_summary(domain, deploy_dir, service_name, port, run_user, source_dir)
    if not args.yes and not prompt_yes_no("Continue with deployment?", True):
        raise SystemExit("Cancelled.")

    sync_project(source_dir, deploy_dir, run_user, run_group)
    sync_python_env(deploy_dir)
    run(["chown", "-R", f"{run_user}:{run_group}", str(deploy_dir)])

    print_step("Writing systemd and Caddy configuration")
    Path("/var/log/caddy").mkdir(parents=True, exist_ok=True)
    service_path = Path("/etc/systemd/system") / f"{service_name}.service"
    site_path = Path("/etc/caddy/sites-enabled") / f"{service_name}.conf"
    main_caddyfile = Path("/etc/caddy/Caddyfile")

    write_text_file(service_path, render_service(service_name, deploy_dir, run_user, run_group, port))
    ensure_caddy_import(main_caddyfile)
    write_text_file(site_path, render_caddy(domain, port, service_name))

    apply_systemd(service_name)
    apply_caddy()

    print("\nDone")
    print("----")
    print(f"App service : systemctl status {service_name}")
    print(f"Caddy config: {site_path}")
    print(f"URL         : https://{domain}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed with exit code {exc.returncode}") from exc
