"""Configuration - loads all env vars and constants."""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")

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

# --- AI ---
A4F_API_URL = os.environ.get("A4F_API_URL", "")
A4F_API_KEY = os.environ.get("A4F_API_KEY", "")
A4F_MODEL = os.environ.get("A4F_MODEL", "")

# --- R2 ---
R2_ENDPOINT = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ.get("R2_BUCKET", "hosting")
R2_REGION = os.environ.get("R2_REGION", "auto")

# --- MongoDB ---
MONGO_URI = os.environ.get("MONGO_URI", "")
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
ALLOWED_EXTENSIONS = {".py", ".zip"}

# --- Docker ---
DOCKER_IMAGE = "python:3.12-slim"

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
