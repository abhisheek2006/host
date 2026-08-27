"""Environment variable manager - ZIP inspection, .env parsing, encryption, dependency detection."""

import ast
import io
import os
import re
import zipfile
from pathlib import Path

from security import encrypt_value, decrypt_value
from database import db_save_env, db_get_envs, db_delete_all_envs
from config import logger


# --- Module -> pip package mapping ---

TELEGRAM_MODULES = {
    'telebot': 'pyTelegramBotAPI',
    'telegram': 'python-telegram-bot',
    'python_telegram_bot': 'python-telegram-bot',
    'aiogram': 'aiogram',
    'pyrogram': 'pyrogram',
    'telethon': 'telethon',
    'telethon.sync': 'telethon',
    'from telethon.sync import telegramclient': 'telethon',
    'telepot': 'telepot',
    'pytg': 'pytg',
    'tgcrypto': 'tgcrypto',
    'telegram_upload': 'telegram-upload',
    'telegram_send': 'telegram-send',
    'telegram_text': 'telegram-text',
    'tl': 'telethon',
    'telegram_utils': 'telegram-utils',
    'telegram_logger': 'telegram-logger',
    'telegram_handlers': 'python-telegram-handlers',
    'telegram_redis': 'telegram-redis',
    'telegram_sqlalchemy': 'telegram-sqlalchemy',
    'telegram_payment': 'telegram-payment',
    'telegram_shop': 'telegram-shop-sdk',
    'pytest_telegram': 'pytest-telegram',
    'telegram_debug': 'telegram-debug',
    'telegram_scraper': 'telegram-scraper',
    'telegram_analytics': 'telegram-analytics',
    'telegram_nlp': 'telegram-nlp-toolkit',
    'telegram_ai': 'telegram-ai',
    'telegram_api': 'telegram-api-client',
    'telegram_web': 'telegram-web-integration',
    'telegram_games': 'telegram-games',
    'telegram_quiz': 'telegram-quiz-bot',
    'telegram_ffmpeg': 'telegram-ffmpeg',
    'telegram_media': 'telegram-media-utils',
    'telegram_2fa': 'telegram-twofa',
    'telegram_crypto': 'telegram-crypto-bot',
    'telegram_i18n': 'telegram-i18n',
    'telegram_translate': 'telegram-translate',
    'bs4': 'beautifulsoup4',
    'requests': 'requests',
    'pillow': 'Pillow',
    'cv2': 'opencv-python',
    'yaml': 'PyYAML',
    'dotenv': 'python-dotenv',
    'dateutil': 'python-dateutil',
    'pandas': 'pandas',
    'numpy': 'numpy',
    'flask': 'Flask',
    'django': 'Django',
    'sqlalchemy': 'SQLAlchemy',
    'asyncio': None,
    'json': None,
    'datetime': None,
    'os': None,
    'sys': None,
    're': None,
    'time': None,
    'math': None,
    'random': None,
    'logging': None,
    'threading': None,
    'subprocess': None,
    'zipfile': None,
    'tempfile': None,
    'shutil': None,
    'sqlite3': None,
    'psutil': 'psutil',
    'atexit': None,
    'aiohttp': 'aiohttp',
    'httpx': 'httpx',
    'selenium': 'selenium',
    'moviepy': 'moviepy',
    'pydub': 'pydub',
    'gtts': 'gTTS',
    'pyttsx3': 'pyttsx3',
    'speech_recognition': 'SpeechRecognition',
    'openai': 'openai',
    'replicate': 'replicate',
    'huggingface_hub': 'huggingface-hub',
    'torch': 'torch',
    'tensorflow': 'tensorflow',
    'sklearn': 'scikit-learn',
    'scipy': 'scipy',
    'matplotlib': 'matplotlib',
    'seaborn': 'seaborn',
    'plotly': 'plotly',
    'bokeh': 'bokeh',
    'fastapi': 'fastapi',
    'uvicorn': 'uvicorn',
    'starlette': 'starlette',
    'aiofiles': 'aiofiles',
    'motor': 'motor',
    'redis': 'redis',
    'celery': 'celery',
    'pytz': 'pytz',
    'dateparser': 'dateparser',
    'validators': 'validators',
    'pyshorteners': 'pyshorteners',
    'qrcode': 'qrcode',
    'pygments': 'Pygments',
    'rich': 'rich',
    'click': 'click',
    'typer': 'typer',
    'pydantic': 'pydantic',
    'fake_useragent': 'fake-useragent',
    'user_agent': 'user-agent',
    'smtplib': None,
    'email': None,
    'ftplib': None,
    'hashlib': None,
    'base64': None,
    'urllib': None,
    'html': None,
    'http': None,
    'socket': None,
    'struct': None,
    'binascii': None,
    'uuid': None,
    'csv': None,
    'xml': None,
    'pickle': None,
    'copy': None,
    'collections': None,
    'functools': None,
    'itertools': None,
    'string': None,
    'textwrap': None,
    'difflib': None,
    'enum': None,
    'dataclasses': None,
    'typing': None,
    'abc': None,
    'contextlib': None,
    'io': None,
    'glob': None,
    'fnmatch': None,
    'pathlib': None,
    'argparse': None,
    'configparser': None,
    'warnings': None,
    'traceback': None,
    'inspect': None,
    'importlib': None,
    'signal': None,
    'multiprocessing': None,
    'concurrent': None,
    'unittest': None,
    'pdb': None,
    'code': None,
    'codeop': None,
    'tokenize': None,
    'keyword': None,
    'ast': None,
    'dis': None,
    'timeit': None,
    'platform': None,
    'ctypes': None,
    'array': None,
    'queue': None,
    'heapq': None,
    'bisect': None,
    'decimal': None,
    'fractions': None,
    'statistics': None,
    'secrets': None,
    'hmac': None,
    'ssl': None,
    'imaplib': None,
    'poplib': None,
    'email.mime': None,
    'email.mime.text': None,
    'email.mime.multipart': None,
    'email.mime.base': None,
}


# --- Dependency Detection ---

def detect_dependencies(code: str | bytes) -> list[str]:
    """Scan Python code for imports, return list of pip packages to install."""
    if isinstance(code, bytes):
        code = code.decode("utf-8", errors="replace")

    imports: set[str] = set()

    # AST-based detection (handles import X, from X import Y)
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
    except SyntaxError:
        pass

    # Regex fallback for edge cases
    for match in re.finditer(r'^\s*(?:import|from)\s+([\w.]+)', code, re.MULTILINE):
        mod = match.group(1).split(".")[0]
        imports.add(mod)

    # Map to pip packages
    packages: set[str] = set()
    for mod in imports:
        pkg = TELEGRAM_MODULES.get(mod)
        if pkg:
            packages.add(pkg)

    return sorted(packages)


def detect_dependencies_from_zip(data: bytes) -> list[str]:
    """Scan all .py files inside a ZIP for imports."""
    combined = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith(".py") and not name.startswith("__"):
                    try:
                        combined.append(zf.read(name).decode("utf-8", errors="replace"))
                    except Exception:
                        continue
    except Exception:
        return ["pyrogram", "tgcrypto"]

    full_code = "\n".join(combined)
    return detect_dependencies(full_code)


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


def _unquote(value: str) -> str:
    """Remove one layer of surrounding quotes and unescape inside quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        quote = value[0]
        inner = value[1:-1]
        if quote == '"':
            return re.sub(r'\\(.)', r'\1', inner)
        return inner.replace("\\'", "'")
    return value


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
            result[key] = _unquote(value)
    return result


def _format_env_line(key: str, value: str) -> str:
    """Format one ENV line so it round-trips (python-dotenv & Docker --env-file compatible).

    Values that contain any character Docker env-file or dotenv would treat
    specially (spaces, '#', '$', quotes, backslashes) are double-quoted and
    escaped so the exact value is preserved.
    """
    # Always quote when the value is empty or contains characters that would
    # be misread as a comment/truncated otherwise.
    if (
        value == ""
        or any(c in value for c in ('"', "'", "\\", " ", "\t", "#", "$", "`", "\n"))
        or value[0] in "="  # prevent KEY==foo being truncated to KEY=
    ):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'{key}="{escaped}"'
    return f"{key}={value}"


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
    lines = [_format_env_line(k, v) for k, v in env_vars.items()]
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
_JS_ENTRY_NAMES = ["bot.js", "main.js", "app.js", "index.js", "run.js", "start.js", "server.js"]


def find_entry(work_dir: Path) -> tuple[str, str]:
    """Find the main bot file in extracted directory. Returns (filename, type)."""
    # 1. Check common Python entry point names
    for name in _ENTRY_NAMES:
        if (work_dir / name).exists():
            return name, "py"

    # 2. Check common JS entry point names
    for name in _JS_ENTRY_NAMES:
        if (work_dir / name).exists():
            return name, "js"

    # 3. Look for any .py with entry point patterns
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

    # 4. Look for any .js with entry point patterns
    for js_file in sorted(work_dir.glob("*.js")):
        try:
            content = js_file.read_text(errors="replace")[:4096]
            if any(kw in content for kw in [
                'require(', 'import ', 'Telegraf', 'Bot(', 'createBot',
                'express()', 'app.listen', 'http.createServer',
            ]):
                return js_file.name, "js"
        except Exception:
            continue

    # 5. Fall back to first .py then .js file at root
    for py_file in sorted(work_dir.glob("*.py")):
        return py_file.name, "py"
    for js_file in sorted(work_dir.glob("*.js")):
        return js_file.name, "js"

    return "", ""


def extract_zip_project(data: bytes, dest: Path) -> tuple[str, str] | None:
    """Safely extract ZIP and detect entry point. Returns (entry, type) or error string."""
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
        js_files = [f.name for f in dest.glob("*.js")]
        all_files = py_files + js_files
        if all_files:
            return f"No entry point found. Files in ZIP: {', '.join(all_files)}"
        return "No .py or .js files found in ZIP"

    # Rename entry to standard name (bot.py or bot.js)
    if etype == "py" and entry != "bot.py":
        src = dest / entry
        dst = dest / "bot.py"
        if dst.exists():
            dst.unlink()
        src.rename(dst)
    elif etype == "js" and entry != "bot.js":
        src = dest / entry
        dst = dest / "bot.js"
        if dst.exists():
            dst.unlink()
        src.rename(dst)

    return None
