"""Database - MongoDB collections and CRUD operations."""

import sys
from datetime import datetime
from pymongo import MongoClient, ASCENDING
from config import MONGO_URI, MONGO_DB_NAME, ADMIN_IDS, logger

mongo_client: MongoClient = None  # type: ignore[assignment]
db = None  # type: ignore[assignment]
bots_col = None  # type: ignore[assignment]
subs_col = None  # type: ignore[assignment]
active_col = None  # type: ignore[assignment]
admins_col = None  # type: ignore[assignment]
envs_col = None  # type: ignore[assignment]
login_col = None  # type: ignore[assignment]
profiles_col = None  # type: ignore[assignment]
_counters_col = None  # type: ignore[assignment]


def init_db() -> None:
    global mongo_client, db, bots_col, subs_col, active_col, admins_col, envs_col, login_col, profiles_col, _counters_col
    try:
        mongo_client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=20000,
        )
        mongo_client.admin.command("ping")
    except Exception as e:
        logger.error("MongoDB connection failed: %s", e)
        print("\n  [FATAL] Cannot connect to MongoDB. Check your MONGO_URI in .env\n")
        sys.exit(1)

    db = mongo_client[MONGO_DB_NAME]
    bots_col = db["bots"]
    subs_col = db["subscriptions"]
    active_col = db["active_users"]
    admins_col = db["admins"]
    envs_col = db["bot_env"]
    login_col = db["login_codes"]
    profiles_col = db["profiles"]
    _counters_col = db["counters"]

    bots_col.create_index([("user_id", ASCENDING)])
    bots_col.create_index([("approval", ASCENDING)])
    bots_col.create_index([("status", ASCENDING)])
    envs_col.create_index([("bot_id", ASCENDING)], unique=False)
    envs_col.create_index([("bot_id", ASCENDING), ("key", ASCENDING)], unique=True)

    for aid in ADMIN_IDS:
        admins_col.update_one({"user_id": aid}, {"$setOnInsert": {"user_id": aid}}, upsert=True)

    logger.info("MongoDB connected: %s", MONGO_DB_NAME)


def _next_id(collection_name: str) -> int:
    result = _counters_col.find_one_and_update(
        {"_id": collection_name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return result["seq"]


def _normalize_bot(row: dict) -> dict:
    if row:
        row["id"] = row["_id"]
        row.setdefault("packages", [])
    return row


# --- Bots ---

def db_add_bot(user_id: int, filename: str, file_type: str, r2_key: str, env_found: bool = False, packages: list[str] | None = None) -> int:
    bot_id = _next_id("bot_id")
    bots_col.insert_one({
        "_id": bot_id,
        "user_id": user_id,
        "filename": filename,
        "file_type": file_type,
        "r2_key": r2_key,
        "status": "stopped",
        "approval": "pending",
        "container_name": None,
        "runtime_path": None,
        "env_file_found": env_found,
        "packages": packages or [],
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    })
    return bot_id


def db_get_bot(bot_id: int, user_id: int) -> dict | None:
    row = bots_col.find_one({"_id": bot_id, "user_id": user_id})
    return _normalize_bot(row) if row else None


def db_get_bot_any(bot_id: int) -> dict | None:
    row = bots_col.find_one({"_id": bot_id})
    return _normalize_bot(row) if row else None


def db_get_user_bots(user_id: int) -> list[dict]:
    rows = bots_col.find({"user_id": user_id}).sort("_id", ASCENDING)
    return [_normalize_bot(r) for r in rows]


def db_update_bot(bot_id: int, **fields) -> None:
    if fields:
        fields["updated_at"] = datetime.utcnow().isoformat()
        bots_col.update_one({"_id": bot_id}, {"$set": fields})


def db_delete_bot(bot_id: int) -> dict | None:
    row = bots_col.find_one({"_id": bot_id})
    if row:
        bots_col.delete_one({"_id": bot_id})
    return _normalize_bot(row) if row else None


# --- Bot Environment Variables ---

def db_save_env(bot_id: int, key: str, encrypted_value: str) -> None:
    envs_col.update_one(
        {"bot_id": bot_id, "key": key},
        {"$set": {"bot_id": bot_id, "key": key, "encrypted_value": encrypted_value, "updated_at": datetime.utcnow().isoformat()}},
        upsert=True,
    )


def db_get_envs(bot_id: int) -> list[dict]:
    return list(envs_col.find({"bot_id": bot_id}, {"_id": 0, "key": 1, "encrypted_value": 1}))


def db_delete_env(bot_id: int, key: str) -> None:
    envs_col.delete_one({"bot_id": bot_id, "key": key})


def db_delete_all_envs(bot_id: int) -> None:
    envs_col.delete_many({"bot_id": bot_id})


# --- Subscriptions ---

def db_save_subscription(user_id: int, expiry: datetime) -> None:
    subs_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "expiry": expiry.isoformat()}},
        upsert=True,
    )


def db_remove_subscription(user_id: int) -> None:
    subs_col.delete_one({"user_id": user_id})


def db_load_subscriptions() -> dict[int, dict]:
    result = {}
    for row in subs_col.find():
        try:
            result[row["user_id"]] = {"expiry": datetime.fromisoformat(row["expiry"])}
        except (ValueError, KeyError):
            pass
    return result


# --- Active Users ---

def db_add_active_user(user_id: int) -> None:
    active_col.update_one({"user_id": user_id}, {"$setOnInsert": {"user_id": user_id}}, upsert=True)


def db_load_active_users() -> set[int]:
    return {row["user_id"] for row in active_col.find()}


# --- Admins ---

def db_add_admin(admin_id: int) -> None:
    admins_col.update_one({"user_id": admin_id}, {"$setOnInsert": {"user_id": admin_id}}, upsert=True)


def db_remove_admin(admin_id: int) -> None:
    admins_col.delete_one({"user_id": admin_id})


def db_load_admins() -> set[int]:
    return {row["user_id"] for row in admins_col.find()}


# --- Counters ---

def db_pending_count() -> int:
    return bots_col.count_documents({"approval": "pending"})


def db_get_pending_bots() -> list[dict]:
    return [_normalize_bot(r) for r in bots_col.find({"approval": "pending"}).sort("_id", ASCENDING)]


def db_get_approved_stopped() -> list[dict]:
    return [_normalize_bot(r) for r in bots_col.find({"approval": "approved", "status": "stopped"}, {"_id": 1})]


def db_count_approved() -> int:
    return bots_col.count_documents({"approval": "approved"})


# --- Web dashboard login codes ---

def db_save_login_code(user_id: int, code: str, expires_at: datetime) -> None:
    """Store a one-time login code for the web dashboard."""
    login_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "code": code, "expires_at": expires_at.isoformat()}},
        upsert=True,
    )


def db_get_login_code(user_id: int) -> dict | None:
    return login_col.find_one({"user_id": user_id}, {"_id": 0, "code": 1, "expires_at": 1})


def db_delete_login_code(user_id: int) -> None:
    login_col.delete_one({"user_id": user_id})


def db_delete_login_code_expired() -> None:
    """Remove expired login codes (housekeeping)."""
    login_col.delete_many({"expires_at": {"$lt": datetime.utcnow().isoformat()}})


# --- User profiles (for the web dashboard) ---

def db_save_profile(user_id: int, first_name: str, username: str | None, bio: str, photo_key: str | None) -> None:
    """Store a user's Telegram profile details for the dashboard."""
    profiles_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "user_id": user_id,
                "first_name": first_name,
                "username": username,
                "bio": bio,
                "photo_key": photo_key,
                "updated_at": datetime.utcnow().isoformat(),
            }
        },
        upsert=True,
    )


def db_get_profile(user_id: int) -> dict | None:
    return profiles_col.find_one({"user_id": user_id}, {"_id": 0})
