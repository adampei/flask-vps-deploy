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
import textwrap
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_PORT_START = 8100
DEFAULT_PORT_END = 8999
DEFAULT_DEPLOY_ROOT = Path("/srv/www")
RSYNC_EXCLUDES = [
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
]


def print_step(message: str) -> None:
    print(f"\n==> {message}")


def run(cmd: list[str], *, cwd: Path | None = None, input_text: str | None = None) -> None:
    pretty = " ".join(shlex.quote(part) for part in cmd)
    print(f"$ {pretty}")
    subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        check=True,
    )


def capture(cmd: list[str], *, cwd: Path | None = None) -> str:
    pretty = " ".join(shlex.quote(part) for part in cmd)
    print(f"$ {pretty}")
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


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


def validate_project_dir(project_dir: Path, label: str) -> Path:
    required = ["pyproject.toml", "app.py", "wsgi.py"]
    missing = [name for name in required if not (project_dir / name).exists()]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"{label} is missing required files: {joined}")
    return project_dir


def resolve_source_dir(value: str | None) -> Path:
    source_dir = Path(value or os.getcwd()).expanduser().resolve()
    return validate_project_dir(source_dir, "Source directory")


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
                "caddy",
            ]
        )
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

    main_caddyfile.write_text(f"{import_line}\n")


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
    site_labels = [f"http://{domain}"]
    if not domain.startswith("www."):
        site_labels.append(f"http://www.{domain}")

    labels = ", ".join(site_labels)
    return textwrap.dedent(
        f"""\
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


def print_summary(
    project_input: str,
    domain: str,
    deploy_dir: Path,
    service_name: str,
    port: int,
    run_user: str,
) -> None:
    print("\nDeployment summary")
    print("------------------")
    print(f"Project     : {project_input}")
    print(f"Deploy dir  : {deploy_dir}")
    print(f"Domain      : {domain}")
    print(f"Service     : {service_name}")
    print(f"Run user    : {run_user}")
    print(f"App port    : 127.0.0.1:{port}")
    print("Origin mode : Caddy HTTP only (Cloudflare handles HTTPS)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interactive VPS deploy wizard for Flask projects using uv, gunicorn, caddy, and systemd."
    )
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
    parser.add_argument("--yes", action="store_true", help="Accept defaults without confirmation.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    require_root()
    if not shutil.which("systemctl"):
        raise SystemExit("systemd is required on the target VPS.")
    if args.source_dir and args.repo_url:
        raise SystemExit("Use either --source-dir or --repo-url, not both.")

    repo_url = args.repo_url
    if repo_url is None:
        repo_url = prompt("Git repository URL (leave blank to read source from current directory and deploy into /srv/www/...)")
    repo_url = repo_url.strip() or None

    source_dir: Path | None = None
    if repo_url:
        project_name = guess_project_name(None, repo_url)
        project_input = repo_url
    else:
        source_dir = resolve_source_dir(args.source_dir)
        project_name = guess_project_name(source_dir, None)
        project_input = str(source_dir)

    domain = validate_domain(args.domain) if args.domain else validate_domain(prompt("Domain"))
    service_name_default = project_name
    service_name = slugify(args.service_name or prompt("Service name", service_name_default))
    deploy_default = str(DEFAULT_DEPLOY_ROOT / service_name)
    deploy_dir = Path(args.deploy_dir or prompt("Deploy directory", deploy_default)).expanduser().resolve()

    if source_dir:
        ensure_not_nested(source_dir, deploy_dir)

    package_manager = detect_package_manager()
    install_system_packages(package_manager)
    install_uv_if_needed()

    if repo_url:
        ensure_git_identity()
        ensure_repo_access(repo_url)

    run_user_input = args.run_user or prompt("Run user", detect_default_run_user())
    run_user, run_group = ensure_user_and_group(run_user_input)

    service_path = Path("/etc/systemd/system") / f"{service_name}.service"
    port = choose_port(args.port, service_path)

    print_summary(project_input, domain, deploy_dir, service_name, port, run_user)
    if not args.yes and not prompt_yes_no("Continue with deployment?", True):
        raise SystemExit("Cancelled.")

    if repo_url:
        project_dir = clone_or_update_repo(repo_url, deploy_dir)
        run(["chown", "-R", f"{run_user}:{run_group}", str(project_dir)])
    else:
        sync_project(source_dir, deploy_dir, run_user, run_group)
        project_dir = deploy_dir

    sync_python_env(project_dir)
    run(["chown", "-R", f"{run_user}:{run_group}", str(project_dir)])

    print_step("Writing systemd and Caddy configuration")
    Path("/var/log/caddy").mkdir(parents=True, exist_ok=True)
    site_path = Path("/etc/caddy/sites-enabled") / f"{service_name}.conf"
    main_caddyfile = Path("/etc/caddy/Caddyfile")

    write_text_file(service_path, render_service(service_name, project_dir, run_user, run_group, port))
    ensure_caddy_import(main_caddyfile)
    write_text_file(site_path, render_caddy(domain, port, service_name))

    apply_systemd(service_name)
    apply_caddy()

    print("\nDone")
    print("----")
    print(f"App service : systemctl status {service_name}")
    print(f"Caddy config: {site_path}")
    print(f"Origin URL  : http://{domain}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed with exit code {exc.returncode}") from exc
