"""Environment variable manager - ZIP inspection, .env parsing, encryption."""

import io
import os
import re
import zipfile
from pathlib import Path

from security import encrypt_value, decrypt_value
from database import db_save_env, db_get_envs, db_delete_all_envs
from config import logger


# --- ZIP Safety ---

def inspect_zip_safety(data: bytes) -> tuple[bool, str]:
    """Check ZIP for path traversal, absolute paths, symlinks. Returns (safe, error)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                name = info.filename
                if os.path.isabs(name):
                    return False, f"Absolute path: {name}"
                if ".." in Path(name).parts:
                    return False, f"Path traversal: {name}"
                if len(name.split("/")) > 10:
                    return False, f"Too deep: {name}"
    except zipfile.BadZipFile:
        return False, "Invalid ZIP file"
    except Exception as e:
        return False, f"ZIP error: {e}"
    return True, ""


def safe_extract_zip(data: bytes, dest: Path) -> None:
    """Extract ZIP with safety checks."""
    safe, err = inspect_zip_safety(data)
    if not safe:
        raise ValueError(err)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest)


# --- .env Detection ---

def find_env_files(data: bytes) -> list[str]:
    """Find .env files in a ZIP. Returns list of paths."""
    env_files = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                name = info.filename
                basename = Path(name).name
                if basename == ".env" or basename.startswith(".env."):
                    env_files.append(name)
    except Exception:
        pass
    return env_files


def pick_primary_env(env_files: list[str]) -> str | None:
    """Pick the primary .env file by priority."""
    if not env_files:
        return None
    priority = [".env", ".env.production", ".env.local"]
    for p in priority:
        for f in env_files:
            if Path(f).name == p:
                return f
    return env_files[0]


# --- .env Parsing ---

_ENV_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<key>[A-Za-z_][A-Za-z0-9_]*)   # key
    \s*=\s*
    (?P<value>                         # value
        "(?:[^"\\]|\\.)*"              # double-quoted
        |
        '(?:[^'\\]|\\.)*'              # single-quoted
        |
        [^\#]*                         # unquoted (up to comment)
    )
    \s*(?:\#.*)?                       # optional comment
    $""",
    re.VERBOSE,
)


def parse_env_file(content: str) -> dict[str, str]:
    """Parse .env content into key-value dict. Safe, no exec/eval."""
    result = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if m:
            key = m.group("key")
            value = m.group("value").strip()
            # Remove surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    return result


# --- Encryption/Decryption ---

def encrypt_env_vars(bot_id: int, env_vars: dict[str, str]) -> int:
    """Encrypt and store environment variables. Returns count stored."""
    count = 0
    for key, value in env_vars.items():
        try:
            encrypted = encrypt_value(value)
            db_save_env(bot_id, key, encrypted)
            count += 1
        except Exception as e:
            logger.error("Failed to encrypt env var %s for bot %d: %s", key, bot_id, e)
    return count


def decrypt_env_vars(bot_id: int) -> dict[str, str]:
    """Retrieve and decrypt environment variables for a bot."""
    envs = db_get_envs(bot_id)
    result = {}
    for entry in envs:
        try:
            result[entry["key"]] = decrypt_value(entry["encrypted_value"])
        except Exception as e:
            logger.error("Failed to decrypt env var %s for bot %d: %s", entry["key"], bot_id, e)
    return result


def get_env_keys(bot_id: int) -> list[str]:
    """Get just the key names for a bot's environment."""
    envs = db_get_envs(bot_id)
    return [e["key"] for e in envs]


def write_env_file(bot_id: int, dest: Path) -> bool:
    """Decrypt env vars and write .env file. Returns True if file written."""
    env_vars = decrypt_env_vars(bot_id)
    if not env_vars:
        return False
    env_path = dest / ".env"
    lines = [f"{k}={v}" for k, v in env_vars.items()]
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    logger.info("Wrote .env for bot %d (%d vars)", bot_id, len(env_vars))
    return True


def delete_env_file(dest: Path) -> None:
    """Remove .env file from disk."""
    env_path = dest / ".env"
    if env_path.exists():
        env_path.unlink()
        logger.info("Deleted .env from %s", dest)


# --- ZIP Extraction with Entry Detection ---

_ENTRY_NAMES = ["bot.py", "main.py", "app.py", "run.py", "start.py", "index.py", "handler.py", "web.py"]


def find_entry(work_dir: Path) -> tuple[str, str]:
    """Find the main bot file in extracted directory."""
    # 1. Check common entry point names
    for name in _ENTRY_NAMES:
        if (work_dir / name).exists():
            return name, "py"

    # 2. Look for any .py with entry point patterns
    for py_file in sorted(work_dir.glob("*.py")):
        try:
            content = py_file.read_text(errors="replace")[:4096]
            if any(kw in content for kw in [
                'if __name__', 'Client(', 'app.run', 'bot.run',
                'create_app', 'application =', 'dp =',
            ]):
                return py_file.name, "py"
        except Exception:
            continue

    # 3. Fall back to first .py file at root
    for py_file in sorted(work_dir.glob("*.py")):
        return py_file.name, "py"

    return "", ""


def extract_zip_project(data: bytes, dest: Path) -> str | None:
    """Safely extract ZIP and detect entry point. Returns error or None."""
    try:
        safe_extract_zip(data, dest)
    except Exception as e:
        return f"ZIP error: {e}"

    # Flatten: if all files are inside a single subfolder, move them up
    entries = [e for e in dest.iterdir() if not e.name.startswith(".") and e.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        sub = entries[0]
        for item in sub.iterdir():
            item.rename(dest / item.name)
        sub.rmdir()

    # Clean __MACOSX junk
    macosx = dest / "__MACOSX"
    if macosx.exists():
        import shutil
        shutil.rmtree(macosx, ignore_errors=True)

    entry, etype = find_entry(dest)
    if not entry:
        py_files = [f.name for f in dest.glob("*.py")]
        if py_files:
            return f"No entry point found. .py files in ZIP: {', '.join(py_files)}"
        return "No .py files found in ZIP"

    if entry != "bot.py":
        src = dest / entry
        dst = dest / "bot.py"
        if dst.exists():
            dst.unlink()
        src.rename(dst)

    return None
