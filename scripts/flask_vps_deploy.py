#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import grp
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_PORT_START = 8100
DEFAULT_PORT_END = 8999
DEFAULT_DEPLOY_ROOT = Path("/srv/www")
DEFAULT_WORKERS = 2
DEFAULT_TIMEOUT = 60
DEFAULT_WSGI_MODULE = "wsgi:app"
DEFAULT_HEALTH_PATH = "/"
INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/adampei/flask-vps-deploy/main/install.sh"
BACKUP_ROOT = Path("/var/lib/flask-vps-deploy/backups")
RSYNC_EXCLUDES = [
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
]
KNOWN_COMMANDS = {"deploy", "redeploy", "status", "logs", "access-logs", "list", "self-update"}
ANSI_RESET = "\033[0m"
STATE_COLORS = {
    "active": "\033[32m",
    "activating": "\033[33m",
    "reloading": "\033[33m",
    "inactive": "\033[31m",
    "failed": "\033[31m",
    "deactivating": "\033[31m",
}


def print_step(message: str) -> None:
    print(f"\n==> {message}")


def format_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def run(cmd: list[str], *, cwd: Path | None = None, input_text: str | None = None) -> None:
    print(f"$ {format_cmd(cmd)}")
    subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        check=True,
    )


def run_optional(cmd: list[str], *, cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    print(f"$ {format_cmd(cmd)}")
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        check=False,
        capture_output=False,
    )


def capture(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> str:
    print(f"$ {format_cmd(cmd)}")
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            cmd,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed.stdout.strip()


def run_shell(cmd: str) -> None:
    print(f"$ {cmd}")
    subprocess.run(cmd, shell=True, check=True)


def require_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("This command must run as root. Use sudo or switch to root.")


def ensure_systemd_available() -> None:
    if not shutil.which("systemctl"):
        raise SystemExit("systemd is required on the target VPS.")


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


def validate_project_dir(project_dir: Path, label: str) -> Path:
    required = ["pyproject.toml", "app.py", "wsgi.py"]
    missing = [name for name in required if not (project_dir / name).exists()]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"{label} is missing required files: {joined}")
    return project_dir


def resolve_source_dir(value: str | None) -> Path:
    source_dir = Path(value or os.getcwd()).expanduser().resolve()
    try:
        return validate_project_dir(source_dir, "Source directory")
    except SystemExit as exc:
        raise SystemExit(
            f"{exc}. Enter a Git repository URL, or run this command inside your project source directory."
        ) from exc


def ensure_user_and_group(user: str) -> tuple[str, str]:
    try:
        user_entry = pwd.getpwnam(user)
    except KeyError as exc:
        raise SystemExit(f"System user does not exist: {user}") from exc
    group = grp.getgrgid(user_entry.pw_gid).gr_name
    return user, group


def normalize_repo_reference(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def guess_project_name(source_dir: Path | None, repo_url: str | None) -> str:
    if repo_url:
        parsed = urlparse(repo_url)
        candidate = Path(parsed.path or repo_url).name
        if candidate.endswith(".git"):
            candidate = candidate[:-4]
        return slugify(candidate)
    if not source_dir:
        raise SystemExit("Unable to determine a project name without a source directory or repository URL.")
    return slugify(source_dir.name)


def ensure_positive(value: int, label: str) -> int:
    if value <= 0:
        raise SystemExit(f"{label} must be greater than zero.")
    return value


def normalize_health_path(value: str) -> str:
    path = value.strip() or DEFAULT_HEALTH_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def get_caddy_version() -> str | None:
    if not shutil.which("caddy"):
        return None
    try:
        return capture(["caddy", "version"])
    except subprocess.CalledProcessError:
        return None


def install_system_packages(package_manager: str) -> None:
    print_step("Installing or updating system packages")
    before_caddy = get_caddy_version()
    if before_caddy:
        print(f"Current Caddy version: {before_caddy}")
    else:
        print("Caddy is not installed yet.")

    if package_manager == "apt-get":
        run(["apt-get", "update"])
        run(
            [
                "apt-get",
                "install",
                "-y",
                "python3",
                "python3-venv",
                "git",
                "curl",
                "rsync",
                "ca-certificates",
                "gnupg",
                "debian-keyring",
                "debian-archive-keyring",
                "apt-transport-https",
            ]
        )
        run_shell(
            "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' "
            "| gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
        )
        run_shell(
            "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' "
            "> /etc/apt/sources.list.d/caddy-stable.list"
        )
        run(["chmod", "o+r", "/usr/share/keyrings/caddy-stable-archive-keyring.gpg"])
        run(["chmod", "o+r", "/etc/apt/sources.list.d/caddy-stable.list"])
        run(["apt-get", "update"])
        run(["apt-get", "install", "-y", "caddy"])
    else:
        run(
            [
                package_manager,
                "install",
                "-y",
                "python3",
                "git",
                "curl",
                "rsync",
                "ca-certificates",
                "caddy",
            ]
        )

    after_caddy = get_caddy_version()
    if not after_caddy:
        raise SystemExit("Caddy install or upgrade did not complete successfully.")
    if before_caddy and before_caddy == after_caddy:
        print(f"Caddy remains at {after_caddy}. This is the latest package version available from the configured repositories.")
    elif before_caddy:
        print(f"Caddy upgraded: {before_caddy} -> {after_caddy}")
    else:
        print(f"Caddy installed: {after_caddy}")


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


def snapshot_deploy_dir(deploy_dir: Path, backup_dir: Path) -> bool:
    if not deploy_dir.exists() or not any(deploy_dir.iterdir()):
        return False
    print_step("Creating deploy backup")
    backup_dir.mkdir(parents=True, exist_ok=True)
    run(["rsync", "-a", "--delete", f"{deploy_dir}/", f"{backup_dir}/"])
    return True


def restore_deploy_dir(backup_dir: Path, deploy_dir: Path) -> None:
    deploy_dir.mkdir(parents=True, exist_ok=True)
    run(["rsync", "-a", "--delete", f"{backup_dir}/", f"{deploy_dir}/"])


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


def restore_file_from_backup(path: Path) -> bool:
    backup = path.with_name(f"{path.name}.bak")
    if not backup.exists():
        return False
    shutil.copy2(backup, path)
    return True


def remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def ensure_caddy_import(main_caddyfile: Path) -> None:
    import_line = "import /etc/caddy/sites-enabled/*"
    if main_caddyfile.exists():
        content = main_caddyfile.read_text()
        if import_line in content:
            return
        if not content.endswith("\n"):
            content += "\n"
        new_content = f"{content}\n{import_line}\n"
        write_text_file(main_caddyfile, new_content)
        return

    write_text_file(main_caddyfile, f"{import_line}\n")


def render_service(
    service_name: str,
    deploy_dir: Path,
    run_user: str,
    run_group: str,
    port: int,
    workers: int,
    timeout: int,
    wsgi_module: str,
) -> str:
    return textwrap.dedent(
        f"""\
        # Managed by flask-vps-deploy
        [Unit]
        Description={service_name} Flask app
        After=network.target

        [Service]
        Type=simple
        User={run_user}
        Group={run_group}
        WorkingDirectory={deploy_dir}
        Environment=PYTHONUNBUFFERED=1
        ExecStart={deploy_dir}/.venv/bin/gunicorn --workers {workers} --bind 127.0.0.1:{port} --timeout {timeout} {wsgi_module}
        Restart=always
        RestartSec=5

        [Install]
        WantedBy=multi-user.target
        """
    )


def render_caddy(domain: str, port: int, log_name: str) -> str:
    site_labels = [f"http://{domain}"]
    if not domain.startswith("www."):
        site_labels.append(f"http://www.{domain}")

    labels = ", ".join(site_labels)
    return textwrap.dedent(
        f"""\
        # Managed by flask-vps-deploy
        {labels} {{
            encode zstd gzip

            header {{
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


def apply_systemd(service_name: str, restart: bool) -> None:
    print_step("Reloading systemd and applying app service")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", service_name])
    if restart:
        run(["systemctl", "restart", service_name])
    else:
        run(["systemctl", "start", service_name])
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


def best_effort_restore_caddy() -> None:
    run_optional(["caddy", "fmt", "--overwrite", "/etc/caddy/Caddyfile"])
    sites_enabled = Path("/etc/caddy/sites-enabled")
    for path in sorted(sites_enabled.glob("*.conf")):
        run_optional(["caddy", "fmt", "--overwrite", str(path)])
    run_optional(["caddy", "validate", "--config", "/etc/caddy/Caddyfile"])
    run_optional(["systemctl", "reload", "caddy"])


def read_existing_service_port(service_path: Path) -> int | None:
    if not service_path.exists():
        return None
    content = service_path.read_text()
    match = re.search(r"--bind\s+127\.0\.0\.1:(\d+)", content)
    if not match:
        return None
    return int(match.group(1))


def collect_reserved_ports(service_path: Path) -> set[int]:
    ports: set[int] = set()
    systemd_dir = Path("/etc/systemd/system")
    for path in systemd_dir.glob("*.service"):
        if path == service_path:
            continue
        port = read_existing_service_port(path)
        if port:
            ports.add(port)
    return ports


def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def find_available_port(start: int, end: int, reserved_ports: set[int]) -> int:
    for port in range(start, end + 1):
        if port in reserved_ports:
            continue
        if not port_listening(port):
            return port
    raise SystemExit(f"No free internal port found between {start} and {end}.")


def choose_port(requested_port: int | None, service_path: Path) -> int:
    existing_port = read_existing_service_port(service_path)
    reserved_ports = collect_reserved_ports(service_path)

    if requested_port is not None:
        if requested_port in reserved_ports:
            raise SystemExit(f"Requested port {requested_port} is already reserved by another managed service.")
        if existing_port and requested_port == existing_port:
            return requested_port
        if port_listening(requested_port):
            raise SystemExit(f"Requested port {requested_port} is already in use.")
        return requested_port

    if existing_port:
        print(f"Reusing existing internal port: {existing_port}")
        return existing_port

    selected = find_available_port(DEFAULT_PORT_START, DEFAULT_PORT_END, reserved_ports)
    print(f"Selected internal port: {selected}")
    return selected


def ensure_git_identity() -> None:
    name = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True, text=True)
    email = subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True)

    current_name = name.stdout.strip() if name.returncode == 0 else ""
    current_email = email.stdout.strip() if email.returncode == 0 else ""
    if current_name and current_email:
        print_step("Global Git identity already configured")
        print(f"user.name : {current_name}")
        print(f"user.email: {current_email}")
        return

    print_step("Configuring global Git identity")
    if not current_name:
        current_name = prompt("Git user.name")
        if not current_name:
            raise SystemExit("Git user.name is required.")
        run(["git", "config", "--global", "user.name", current_name])
    if not current_email:
        current_email = prompt("Git user.email")
        if not current_email:
            raise SystemExit("Git user.email is required.")
        run(["git", "config", "--global", "user.email", current_email])


def repo_accessible(repo_url: str) -> bool:
    completed = subprocess.run(["git", "ls-remote", repo_url], capture_output=True, text=True)
    return completed.returncode == 0


def is_https_github_repo(repo_url: str) -> bool:
    parsed = urlparse(repo_url)
    return parsed.scheme == "https" and parsed.netloc.lower() == "github.com"


def configure_github_credentials(repo_url: str) -> None:
    print_step("Configuring GitHub credentials for repository access")
    username = prompt("GitHub username")
    if not username:
        raise SystemExit("GitHub username is required to store credentials.")
    token = getpass.getpass("GitHub token or password: ").strip()
    if not token:
        raise SystemExit("GitHub token or password is required to store credentials.")

    parsed = urlparse(repo_url)
    run(["git", "config", "--global", "credential.helper", "store"])
    credential_payload = (
        f"protocol={parsed.scheme}\n"
        f"host={parsed.netloc}\n"
        f"username={username}\n"
        f"password={token}\n\n"
    )
    run(["git", "credential", "approve"], input_text=credential_payload)


def ensure_repo_access(repo_url: str) -> None:
    if repo_accessible(repo_url):
        return
    if not is_https_github_repo(repo_url):
        raise SystemExit(
            "Unable to access the repository. Check the URL, SSH keys, or Git credentials on this VPS."
        )
    configure_github_credentials(repo_url)
    if not repo_accessible(repo_url):
        raise SystemExit("GitHub repository access still failed after storing credentials.")


def clone_or_update_repo(repo_url: str, deploy_dir: Path) -> Path:
    print_step("Preparing project repository")
    deploy_dir.parent.mkdir(parents=True, exist_ok=True)

    if deploy_dir.exists() and (deploy_dir / ".git").exists():
        origin = capture(["git", "-C", str(deploy_dir), "remote", "get-url", "origin"])
        if normalize_repo_reference(origin) != normalize_repo_reference(repo_url):
            raise SystemExit(
                f"Deploy directory already points to a different repository: {origin}"
            )
        run(["git", "-C", str(deploy_dir), "fetch", "--all", "--prune"])
        run(["git", "-C", str(deploy_dir), "pull", "--ff-only"])
    else:
        if deploy_dir.exists():
            if any(deploy_dir.iterdir()):
                raise SystemExit(
                    f"Deploy directory exists and is not an empty git checkout: {deploy_dir}"
                )
            deploy_dir.rmdir()
        run(["git", "clone", repo_url, str(deploy_dir)])

    return validate_project_dir(deploy_dir, "Repository checkout")


def get_repo_url_from_checkout(deploy_dir: Path) -> str | None:
    git_dir = deploy_dir / ".git"
    if not git_dir.exists():
        return None
    repo_url = capture(["git", "-C", str(deploy_dir), "remote", "get-url", "origin"], check=False)
    return repo_url or None


def curl_succeeds(url: str, *, host: str | None = None, timeout: int = 5) -> bool:
    cmd = ["curl", "-fsS", "-o", "/dev/null", "--max-time", str(timeout), url]
    if host:
        cmd.extend(["-H", f"Host: {host}"])
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return completed.returncode == 0


def run_health_checks(port: int, domain: str, health_path: str, attempts: int = 10, delay: float = 1.0) -> None:
    print_step("Running post-deploy health checks")
    app_url = f"http://127.0.0.1:{port}{health_path}"
    proxy_url = f"http://127.0.0.1{health_path}"

    for attempt in range(1, attempts + 1):
        app_ok = curl_succeeds(app_url)
        proxy_ok = curl_succeeds(proxy_url, host=domain)
        print(f"Health check attempt {attempt}/{attempts}: app={'ok' if app_ok else 'fail'}, proxy={'ok' if proxy_ok else 'fail'}")
        if app_ok and proxy_ok:
            return
        time.sleep(delay)

    raise SystemExit(
        f"Health checks failed for {domain} after deployment. Checked {app_url} and {proxy_url} with Host={domain}."
    )


def rollback_deploy(
    *,
    service_name: str,
    deploy_dir: Path,
    deploy_backup_dir: Path,
    had_deploy_backup: bool,
    service_path: Path,
    service_existed_before: bool,
    site_path: Path,
    site_existed_before: bool,
    main_caddyfile: Path,
    main_caddyfile_existed_before: bool,
) -> None:
    print_step("Deployment failed, attempting rollback")

    if had_deploy_backup:
        restore_deploy_dir(deploy_backup_dir, deploy_dir)
    else:
        print("No previous deploy backup found. Code directory will be left as-is.")

    if service_existed_before:
        restore_file_from_backup(service_path)
    else:
        run_optional(["systemctl", "disable", "--now", service_name])
        remove_file_if_exists(service_path)

    if site_existed_before:
        restore_file_from_backup(site_path)
    else:
        remove_file_if_exists(site_path)

    if main_caddyfile_existed_before:
        restore_file_from_backup(main_caddyfile)
    else:
        remove_file_if_exists(main_caddyfile)

    run_optional(["systemctl", "daemon-reload"])
    if service_existed_before:
        run_optional(["systemctl", "restart", service_name])
    best_effort_restore_caddy()


def print_summary(
    project_input: str,
    domain: str,
    deploy_dir: Path,
    service_name: str,
    port: int,
    run_user: str,
    workers: int,
    timeout: int,
    wsgi_module: str,
    health_path: str,
) -> None:
    print("\nDeployment summary")
    print("------------------")
    print(f"Project     : {project_input}")
    print(f"Deploy dir  : {deploy_dir}")
    print(f"Domain      : {domain}")
    print(f"Service     : {service_name}")
    print(f"Run user    : {run_user}")
    print(f"App port    : 127.0.0.1:{port}")
    print(f"WSGI module : {wsgi_module}")
    print(f"Workers     : {workers}")
    print(f"Timeout     : {timeout}")
    print(f"Health path : {health_path}")
    print("Origin mode : Caddy HTTP only (Cloudflare handles HTTPS)")


def service_unit_name(service_name: str) -> str:
    return service_name if service_name.endswith(".service") else f"{service_name}.service"


def service_state(service_name: str) -> str:
    completed = subprocess.run(
        ["systemctl", "is-active", service_unit_name(service_name)],
        capture_output=True,
        text=True,
        check=False,
    )
    return (completed.stdout or completed.stderr).strip() or "unknown"


def colorize_state(value: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return value
    color = STATE_COLORS.get(value)
    if not color:
        return value
    return f"{color}{value}{ANSI_RESET}"


def is_managed_service(service_path: Path) -> bool:
    content = service_path.read_text()
    return "Managed by flask-vps-deploy" in content or (
        "Flask app" in content and ".venv/bin/gunicorn" in content
    )


def extract_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def get_site_domain(service_name: str) -> str | None:
    site_path = Path("/etc/caddy/sites-enabled") / f"{service_name}.conf"
    if not site_path.exists():
        return None
    content = site_path.read_text()
    return extract_match(r"http://([^,\s{]+)", content)


def get_service_info(service_name: str) -> dict[str, str | int | None]:
    normalized = service_unit_name(service_name)
    service_path = Path("/etc/systemd/system") / normalized
    if not service_path.exists():
        raise SystemExit(f"Service file not found: {service_path}")

    content = service_path.read_text()
    port = read_existing_service_port(service_path)
    info: dict[str, str | int | None] = {
        "service_name": service_path.stem,
        "service_path": str(service_path),
        "deploy_dir": extract_match(r"^WorkingDirectory=(.+)$", content),
        "run_user": extract_match(r"^User=(.+)$", content),
        "domain": get_site_domain(service_path.stem),
        "port": port,
        "workers": extract_match(r"--workers\s+(\d+)", content),
        "timeout": extract_match(r"--timeout\s+(\d+)", content),
        "wsgi_module": extract_match(r"--timeout\s+\d+\s+(\S+:\S+)\s*$", content),
        "state": service_state(service_path.stem),
    }
    return info


def iter_managed_services() -> list[dict[str, str | int | None]]:
    services: list[dict[str, str | int | None]] = []
    for path in sorted(Path("/etc/systemd/system").glob("*.service")):
        if not is_managed_service(path):
            continue
        services.append(get_service_info(path.stem))
    return services


def select_service_interactively(services: list[dict[str, str | int | None]]) -> str:
    if not services:
        raise SystemExit("No flask-vps-deploy managed sites found.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Interactive service selection requires a TTY. Pass a service name explicitly.")

    import curses

    def draw_line(stdscr: curses.window, row: int, text: str, width: int, selected: bool) -> None:
        if selected:
            stdscr.attron(curses.A_REVERSE)
        stdscr.addnstr(row, 0, text, max(1, width - 1))
        if selected:
            stdscr.attroff(curses.A_REVERSE)

    def selector(stdscr: curses.window) -> str:
        index = 0
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            header = "Select a deployed site to redeploy (Up/Down to move, Enter to confirm, q to quit)"
            draw_line(stdscr, 0, header, width, False)

            visible_rows = max(1, height - 2)
            start = max(0, min(index - visible_rows + 1, max(0, len(services) - visible_rows)))
            visible = services[start : start + visible_rows]

            for offset, info in enumerate(visible, start=1):
                actual_index = start + offset - 1
                line = (
                    f"{info['service_name']} [{info['state']}] "
                    f"{info['domain'] or '-'} -> {info['deploy_dir'] or '-'}"
                )
                draw_line(stdscr, offset, line, width, actual_index == index)

            stdscr.refresh()
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                index = (index - 1) % len(services)
            elif key in (curses.KEY_DOWN, ord("j")):
                index = (index + 1) % len(services)
            elif key in (10, 13, curses.KEY_ENTER):
                return str(services[index]["service_name"])
            elif key in (27, ord("q")):
                raise SystemExit("Cancelled.")

    return curses.wrapper(selector)


def build_redeploy_context(service_name: str, health_path_override: str | None) -> dict[str, str | int | Path | None]:
    info = get_service_info(service_name)
    deploy_dir_value = info.get("deploy_dir")
    domain_value = info.get("domain")
    run_user_value = info.get("run_user")
    wsgi_value = info.get("wsgi_module")

    if not deploy_dir_value:
        raise SystemExit(f"Cannot infer deploy directory from {service_name}.")
    if not domain_value:
        raise SystemExit(f"Cannot infer domain from Caddy config for {service_name}.")
    if not run_user_value:
        raise SystemExit(f"Cannot infer run user from {service_name}.")
    if not wsgi_value:
        raise SystemExit(f"Cannot infer WSGI module from {service_name}.")

    deploy_dir = Path(str(deploy_dir_value)).expanduser().resolve()
    repo_url = get_repo_url_from_checkout(deploy_dir)
    if not repo_url:
        raise SystemExit(
            f"Cannot infer repository URL from {deploy_dir}. redeploy currently requires a git checkout with origin configured."
        )

    workers = int(str(info.get("workers") or DEFAULT_WORKERS))
    timeout = int(str(info.get("timeout") or DEFAULT_TIMEOUT))
    port = int(str(info.get("port") or 0)) or None
    health_path = normalize_health_path(health_path_override or DEFAULT_HEALTH_PATH)
    run_user, run_group = ensure_user_and_group(str(run_user_value))

    return {
        "project_input": repo_url,
        "repo_url": repo_url,
        "source_dir": None,
        "domain": str(domain_value),
        "deploy_dir": deploy_dir,
        "service_name": str(info["service_name"]),
        "run_user": run_user,
        "run_group": run_group,
        "requested_port": port,
        "workers": workers,
        "timeout": timeout,
        "wsgi_module": str(wsgi_value),
        "health_path": health_path,
    }


def execute_deploy(
    *,
    project_input: str,
    repo_url: str | None,
    source_dir: Path | None,
    domain: str,
    deploy_dir: Path,
    service_name: str,
    run_user: str,
    run_group: str,
    requested_port: int | None,
    workers: int,
    timeout: int,
    wsgi_module: str,
    health_path: str,
    skip_health_check: bool,
    confirm: bool,
) -> None:
    if source_dir:
        ensure_not_nested(source_dir, deploy_dir)

    package_manager = detect_package_manager()
    install_system_packages(package_manager)
    install_uv_if_needed()

    if repo_url:
        ensure_git_identity()
        ensure_repo_access(repo_url)

    service_path = Path("/etc/systemd/system") / f"{service_name}.service"
    site_path = Path("/etc/caddy/sites-enabled") / f"{service_name}.conf"
    main_caddyfile = Path("/etc/caddy/Caddyfile")
    port = choose_port(requested_port, service_path)

    print_summary(
        project_input,
        domain,
        deploy_dir,
        service_name,
        port,
        run_user,
        workers,
        timeout,
        wsgi_module,
        health_path,
    )
    if confirm and not prompt_yes_no("Continue with deployment?", True):
        raise SystemExit("Cancelled.")

    service_existed_before = service_path.exists()
    site_existed_before = site_path.exists()
    main_caddyfile_existed_before = main_caddyfile.exists()
    deploy_backup_dir = BACKUP_ROOT / service_name
    had_deploy_backup = snapshot_deploy_dir(deploy_dir, deploy_backup_dir)

    try:
        if repo_url:
            project_dir = clone_or_update_repo(repo_url, deploy_dir)
            run(["chown", "-R", f"{run_user}:{run_group}", str(project_dir)])
        else:
            if not source_dir:
                raise SystemExit("A source directory is required when repo_url is not provided.")
            sync_project(source_dir, deploy_dir, run_user, run_group)
            project_dir = deploy_dir

        sync_python_env(project_dir)
        run(["chown", "-R", f"{run_user}:{run_group}", str(project_dir)])

        print_step("Writing systemd and Caddy configuration")
        Path("/var/log/caddy").mkdir(parents=True, exist_ok=True)
        write_text_file(
            service_path,
            render_service(
                service_name,
                project_dir,
                run_user,
                run_group,
                port,
                workers,
                timeout,
                wsgi_module,
            ),
        )
        ensure_caddy_import(main_caddyfile)
        write_text_file(site_path, render_caddy(domain, port, service_name))

        apply_systemd(service_name, restart=service_existed_before)
        apply_caddy()

        if not skip_health_check:
            run_health_checks(port, domain, health_path)

        print("\nDone")
        print("----")
        print(f"App service : systemctl status {service_name}")
        print(f"Caddy config: {site_path}")
        print(f"Origin URL  : http://{domain}")
    except (SystemExit, subprocess.CalledProcessError):
        rollback_deploy(
            service_name=service_name,
            deploy_dir=deploy_dir,
            deploy_backup_dir=deploy_backup_dir,
            had_deploy_backup=had_deploy_backup,
            service_path=service_path,
            service_existed_before=service_existed_before,
            site_path=site_path,
            site_existed_before=site_existed_before,
            main_caddyfile=main_caddyfile,
            main_caddyfile_existed_before=main_caddyfile_existed_before,
        )
        raise


def command_list(_: argparse.Namespace) -> None:
    services = iter_managed_services()
    if not services:
        print("No flask-vps-deploy managed sites found.")
        return

    print("Managed sites")
    print("-------------")
    for info in services:
        print(f"{info['service_name']} [{colorize_state(str(info['state']))}]")
        print(f"  domain    : {info['domain'] or '-'}")
        print(f"  deploy dir: {info['deploy_dir'] or '-'}")
        print(f"  port      : {info['port'] or '-'}")
        print(f"  run user  : {info['run_user'] or '-'}")


def command_status(args: argparse.Namespace) -> None:
    if not args.service_name:
        command_list(args)
        return

    info = get_service_info(args.service_name)
    print("Service summary")
    print("---------------")
    print(f"Service    : {info['service_name']}")
    print(f"State      : {colorize_state(str(info['state']))}")
    print(f"Domain     : {info['domain'] or '-'}")
    print(f"Deploy dir : {info['deploy_dir'] or '-'}")
    print(f"Port       : {info['port'] or '-'}")
    print(f"Run user   : {info['run_user'] or '-'}")
    print(f"Workers    : {info['workers'] or '-'}")
    print(f"Timeout    : {info['timeout'] or '-'}")
    print(f"WSGI module: {info['wsgi_module'] or '-'}")
    run(["systemctl", "status", service_unit_name(args.service_name), "--no-pager"])


def caddy_access_log_path(service_name: str) -> Path:
    normalized = service_unit_name(service_name)
    service_stem = Path(normalized).stem
    return Path("/var/log/caddy") / f"{service_stem}.access.log"


def command_logs(args: argparse.Namespace) -> None:
    cmd = ["journalctl", "-u", service_unit_name(args.service_name), "-n", str(args.lines)]
    if args.follow:
        cmd.append("-f")
    else:
        cmd.append("--no-pager")
    run(cmd)


def command_access_logs(args: argparse.Namespace) -> None:
    log_path = caddy_access_log_path(args.service_name)
    if not log_path.exists():
        raise SystemExit(f"Caddy access log not found: {log_path}")
    cmd = ["tail", "-n", str(args.lines)]
    if args.follow:
        cmd.append("-f")
    cmd.append(str(log_path))
    run(cmd)


def command_self_update(_: argparse.Namespace) -> None:
    require_root()
    print_step("Updating flask-vps-deploy")
    run_shell(f"curl -fsSL {INSTALL_SCRIPT_URL} | bash")


def command_deploy(args: argparse.Namespace) -> None:
    require_root()
    ensure_systemd_available()
    if args.source_dir and args.repo_url:
        raise SystemExit("Use either --source-dir or --repo-url, not both.")

    workers = ensure_positive(args.workers, "--workers")
    timeout = ensure_positive(args.timeout, "--timeout")
    health_path = normalize_health_path(args.health_path)

    repo_url = args.repo_url
    if repo_url is None:
        repo_url = prompt("Git repository URL (leave blank to use current directory as the source)")
    repo_url = repo_url.strip() or None

    source_dir: Path | None = None
    if repo_url:
        project_name = guess_project_name(None, repo_url)
        project_input = repo_url
    else:
        source_dir = resolve_source_dir(args.source_dir)
        project_name = guess_project_name(source_dir, None)
        project_input = str(source_dir)

    service_name_default = project_name
    deploy_default = str(DEFAULT_DEPLOY_ROOT / service_name_default)
    deploy_dir = Path(args.deploy_dir or prompt("Deploy directory", deploy_default)).expanduser().resolve()
    domain = validate_domain(args.domain) if args.domain else validate_domain(prompt("Domain"))
    service_name = slugify(args.service_name or prompt("Service name", service_name_default))

    run_user_input = args.run_user or prompt("Run user", detect_default_run_user())
    run_user, run_group = ensure_user_and_group(run_user_input)

    execute_deploy(
        project_input=project_input,
        repo_url=repo_url,
        source_dir=source_dir,
        domain=domain,
        deploy_dir=deploy_dir,
        service_name=service_name,
        run_user=run_user,
        run_group=run_group,
        requested_port=args.port,
        workers=workers,
        timeout=timeout,
        wsgi_module=args.wsgi_module,
        health_path=health_path,
        skip_health_check=args.skip_health_check,
        confirm=not args.yes,
    )


def command_redeploy(args: argparse.Namespace) -> None:
    require_root()
    ensure_systemd_available()

    service_name = args.service_name
    if not service_name:
        service_name = select_service_interactively(iter_managed_services())

    context = build_redeploy_context(service_name, args.health_path)
    execute_deploy(
        project_input=str(context["project_input"]),
        repo_url=str(context["repo_url"]),
        source_dir=None,
        domain=str(context["domain"]),
        deploy_dir=Path(context["deploy_dir"]),
        service_name=str(context["service_name"]),
        run_user=str(context["run_user"]),
        run_group=str(context["run_group"]),
        requested_port=int(context["requested_port"]) if context["requested_port"] is not None else None,
        workers=int(context["workers"]),
        timeout=int(context["timeout"]),
        wsgi_module=str(context["wsgi_module"]),
        health_path=str(context["health_path"]),
        skip_health_check=args.skip_health_check,
        confirm=not args.yes,
    )


def build_deploy_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-dir",
        help="Local project source directory. Defaults to the current directory when --repo-url is not provided, then syncs into the deploy directory.",
    )
    parser.add_argument("--repo-url", help="Git repository URL to clone or pull before deployment.")
    parser.add_argument("--domain", help="Primary domain for Caddy.")
    parser.add_argument("--deploy-dir", help="Target deploy directory on the VPS.")
    parser.add_argument("--service-name", help="systemd service name. Defaults to the project name.")
    parser.add_argument("--port", type=int, help="Internal Gunicorn port. Defaults to an auto-selected free port.")
    parser.add_argument("--run-user", help="Linux user for the app service. Defaults to www-data or caddy.")
    parser.add_argument("--wsgi-module", default=DEFAULT_WSGI_MODULE, help=f"Gunicorn app entrypoint. Default: {DEFAULT_WSGI_MODULE}")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Gunicorn worker count. Default: {DEFAULT_WORKERS}")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Gunicorn timeout in seconds. Default: {DEFAULT_TIMEOUT}")
    parser.add_argument("--health-path", default=DEFAULT_HEALTH_PATH, help=f"Path used for post-deploy health checks. Default: {DEFAULT_HEALTH_PATH}")
    parser.add_argument("--skip-health-check", action="store_true", help="Skip the post-deploy health check and rollback logic.")
    parser.add_argument("--yes", action="store_true", help="Accept defaults without confirmation.")


def build_redeploy_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("service_name", nargs="?", help="Existing service name. If omitted, choose interactively.")
    parser.add_argument("--health-path", help="Override the health check path for this redeploy. Default: /")
    parser.add_argument("--skip-health-check", action="store_true", help="Skip the post-deploy health check and rollback logic.")
    parser.add_argument("--yes", action="store_true", help="Accept defaults without confirmation.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flask VPS deploy and maintenance toolkit.")
    subparsers = parser.add_subparsers(dest="command")

    deploy_parser = subparsers.add_parser("deploy", help="Deploy or update a Flask site.")
    build_deploy_parser(deploy_parser)

    redeploy_parser = subparsers.add_parser("redeploy", help="Redeploy an existing site by inferring its current configuration.")
    build_redeploy_parser(redeploy_parser)

    status_parser = subparsers.add_parser("status", help="Show status for a deployed site.")
    status_parser.add_argument("service_name", nargs="?", help="Service name. If omitted, all managed sites are listed.")

    logs_parser = subparsers.add_parser("logs", help="Show journald logs for a deployed site.")
    logs_parser.add_argument("service_name", help="Service name.")
    logs_parser.add_argument("-n", "--lines", type=int, default=100, help="Number of log lines to show. Default: 100")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow logs in real time.")

    access_logs_parser = subparsers.add_parser("access-logs", help="Show Caddy access logs for a deployed site.")
    access_logs_parser.add_argument("service_name", help="Service name.")
    access_logs_parser.add_argument("-n", "--lines", type=int, default=100, help="Number of log lines to show. Default: 100")
    access_logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow logs in real time.")

    subparsers.add_parser("list", help="List all managed sites.")
    subparsers.add_parser("self-update", help="Update flask-vps-deploy itself from GitHub.")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if raw_args and raw_args[0] in {"-h", "--help"}:
        return parser.parse_args(raw_args)
    if not raw_args or raw_args[0].startswith("-") or raw_args[0] not in KNOWN_COMMANDS:
        raw_args = ["deploy", *raw_args]
    return parser.parse_args(raw_args)


def main() -> None:
    args = parse_args()

    if args.command == "deploy":
        command_deploy(args)
        return
    if args.command == "redeploy":
        command_redeploy(args)
        return
    if args.command == "status":
        command_status(args)
        return
    if args.command == "logs":
        command_logs(args)
        return
    if args.command == "access-logs":
        command_access_logs(args)
        return
    if args.command == "list":
        command_list(args)
        return
    if args.command == "self-update":
        command_self_update(args)
        return

    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed with exit code {exc.returncode}") from exc
