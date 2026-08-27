"""Configuration - loads all env vars and constants."""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# --- Validate required env vars ---
_PLACEHOLDERS = {
    "your-mongodb-connection-string",
    "your-bot-token-here",
    "your-api-id",
    "your-api-hash",
    "your-r2-access-key",
    "your-r2-secret-key",
    "your-r2-endpoint",
    "run-setup-vps-to-generate",
    "",
}


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val or val.lower() in _PLACEHOLDERS:
        print(f"\n  [FATAL] Environment variable '{key}' is missing or still a placeholder.")
        print(f"  Edit your .env file and set a real value.\n")
        sys.exit(1)
    return val


# --- Telegram ---
BOT_TOKEN = _require("BOT_TOKEN")
API_ID = int(_require("API_ID"))
API_HASH = _require("API_HASH")

# --- Access control ---
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
ADMIN_IDS_raw = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS: set[int] = set()
if ADMIN_IDS_raw:
    for x in ADMIN_IDS_raw.split(","):
        x = x.strip()
        if x:
            ADMIN_IDS.add(int(x))
ADMIN_IDS.add(OWNER_ID)

YOUR_USERNAME = os.environ.get("YOUR_USERNAME", "@admin")
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL", "")

# Web dashboard URL (shown to users when logging in to the dashboard)
WEB_URL = os.environ.get("WEB_URL", "http://localhost:8000")

# --- AI ---
A4F_API_URL = os.environ.get("A4F_API_URL", "")
A4F_API_KEY = os.environ.get("A4F_API_KEY", "")
A4F_MODEL = os.environ.get("A4F_MODEL", "")

# --- R2 ---
R2_ENDPOINT = _require("R2_ENDPOINT")
R2_ACCESS_KEY_ID = _require("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = _require("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.environ.get("R2_BUCKET", "hosting")
R2_REGION = os.environ.get("R2_REGION", "auto")

# --- MongoDB ---
MONGO_URI = _require("MONGO_URI")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "hostbot")

# --- Encryption ---
ENV_ENCRYPTION_KEY = os.environ.get("ENV_ENCRYPTION_KEY", "")

# --- Limits ---
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_MEMORY = os.environ.get("MAX_MEMORY", "512m")
MAX_CPUS = os.environ.get("MAX_CPUS", "1")
MAX_PIDS = int(os.environ.get("MAX_PIDS", "128"))
MAX_BOTS_PER_USER = int(os.environ.get("MAX_BOTS_PER_USER", "5"))
FREE_USER_LIMIT = int(os.environ.get("FREE_USER_LIMIT", "2"))
SUBSCRIBED_USER_LIMIT = int(os.environ.get("SUBSCRIBED_USER_LIMIT", "15"))
ADMIN_LIMIT = 999
OWNER_LIMIT = float("inf")

# --- Paths ---
BOT_START_TIME = datetime.now()
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
RUNTIME_DIR = PROJECT_DIR / "runtime"

DATA_DIR.mkdir(exist_ok=True)
RUNTIME_DIR.mkdir(exist_ok=True)

# --- Allowed uploads ---
ALLOWED_EXTENSIONS = {".py", ".js", ".zip"}

# --- Docker ---
DOCKER_IMAGE = "python:3.12-slim"
NODE_IMAGE = "node:20-slim"

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
