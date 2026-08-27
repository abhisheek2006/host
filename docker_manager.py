"""Docker manager - container lifecycle, security constraints."""

import subprocess
from pathlib import Path
from config import DOCKER_IMAGE, NODE_IMAGE, MAX_MEMORY, MAX_CPUS, MAX_PIDS, logger

HOSTBOT_IMAGE = "hostbot-python"


def build_custom_image() -> None:
    """Build the custom Python image with build-essential pre-installed. Skips if already exists."""
    r = subprocess.run(
        ["docker", "image", "inspect", HOSTBOT_IMAGE],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode == 0:
        logger.info("Custom image %s already exists, skipping build", HOSTBOT_IMAGE)
        return

    dockerfile = Path(__file__).parent / "Dockerfile"
    if not dockerfile.exists():
        return
    subprocess.Popen(
        ["docker", "build", "-t", HOSTBOT_IMAGE, "-f", str(dockerfile), "."],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(dockerfile.parent),
    )
    logger.info("Building custom image %s in background...", HOSTBOT_IMAGE)


def make_container_name(user_id: int, bot_id: int) -> str:
    return f"hostbot_{user_id}_{bot_id}"


def docker_run(
    container_name: str, work_dir: Path, env_file_path: Path | None = None,
    packages: list[str] | None = None, file_type: str = "py",
    memory: str = MAX_MEMORY, cpus: str = MAX_CPUS, pids: int = MAX_PIDS,
) -> str:
    """Start a Docker container. Returns container ID or raises."""
    install_parts: list[str] = []
    image = HOSTBOT_IMAGE
    # `-u` = unbuffered output so the bot's runtime logs show up in docker logs
    entry_cmd = "python -u bot.py"

    if file_type == "js":
        image = NODE_IMAGE
        entry_cmd = "node bot.js"
        pkg_path = work_dir / "package.json"
        if pkg_path.exists():
            install_parts.append("npm install --no-audit --no-fund")
        if packages:
            install_parts.append("npm install --no-audit --no-fund " + " ".join(packages))
    else:
        # Install from requirements.txt if present in the user's project
        req_path = work_dir / "requirements.txt"
        if req_path.exists():
            install_parts.append("pip install --no-cache-dir -r requirements.txt")
        if packages:
            install_parts.append("pip install --no-cache-dir " + " ".join(packages))

    install_cmd = " && ".join(install_parts)
    if install_cmd:
        install_cmd += " && "

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--memory", memory,
        "--cpus", cpus,
        "--pids-limit", str(pids),
        "--network", "bridge",
    ]

    if env_file_path and env_file_path.exists():
        cmd.extend(["--env-file", str(env_file_path)])

    cmd.extend([
        "-v", f"{work_dir}:/app:ro",
        "-w", "/app",
        image,
        "sh", "-c",
        f"{install_cmd}{entry_cmd}",
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


def docker_running(name: str) -> bool:
    """True if the named container exists and is currently running."""
    r = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", name],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def docker_logs(name: str, tail: int = 50) -> str:
    r = subprocess.run(
        ["docker", "logs", "--tail", str(tail), name],
        capture_output=True, text=True, timeout=10,
    )
    return r.stdout + r.stderr
