"""Docker manager - container lifecycle, security constraints."""

import subprocess
from pathlib import Path
from config import DOCKER_IMAGE, MAX_MEMORY, MAX_CPUS, MAX_PIDS, logger


def make_container_name(user_id: int, bot_id: int) -> str:
    return f"hostbot_{user_id}_{bot_id}"


def docker_run(
    container_name: str, work_dir: Path, env_file_path: Path | None = None,
    packages: list[str] | None = None,
    memory: str = MAX_MEMORY, cpus: str = MAX_CPUS, pids: int = MAX_PIDS,
) -> str:
    """Start a Docker container. Returns container ID or raises."""
    install_parts: list[str] = []

    # Install from requirements.txt if present
    req_path = work_dir / "requirements.txt"
    if req_path.exists():
        install_parts.append("pip install --no-cache-dir -r requirements.txt")

    # Install detected dependencies
    if packages:
        install_parts.append("pip install --no-cache-dir " + " ".join(packages))

    pip_cmd = " && ".join(install_parts)
    if pip_cmd:
        pip_cmd += " && "

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--memory", memory,
        "--cpus", cpus,
        "--pids-limit", str(pids),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:size=100m",
        "--network", "bridge",
    ]

    if env_file_path and env_file_path.exists():
        cmd.extend(["--env-file", str(env_file_path)])

    cmd.extend([
        "-v", f"{work_dir}:/app:ro",
        "-w", "/app",
        DOCKER_IMAGE,
        "sh", "-c",
        f"{pip_cmd}python bot.py",
    ])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def docker_stop(name: str) -> None:
    subprocess.run(["docker", "stop", name], capture_output=True, timeout=30)
    subprocess.run(["docker", "rm", name], capture_output=True, timeout=30)


def docker_exists(name: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True, timeout=10
    )
    return r.returncode == 0


def docker_logs(name: str, tail: int = 50) -> str:
    r = subprocess.run(
        ["docker", "logs", "--tail", str(tail), name],
        capture_output=True, text=True, timeout=10,
    )
    return r.stdout + r.stderr
