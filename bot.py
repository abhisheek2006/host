#!/usr/bin/env python3
"""Telegram Bot Hosting Platform.

Pyrogram + Docker + Cloudflare R2 + MongoDB + Fernet Encryption.
Upload .py/.zip files, admin approves, run in Docker containers on VPS.
Environment variables are encrypted and stored in the database.
"""

import asyncio
import io
import os
import sys
import uuid
import time
import shutil
import logging
import threading
import zipfile
import requests
from pathlib import Path
from datetime import datetime, timedelta

# Python 3.14 removed auto-creation of the event loop.
# Pyrogram calls asyncio.get_event_loop() at import time.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, enums, filters, types

from config import (
    BOT_TOKEN, API_ID, API_HASH, OWNER_ID, ADMIN_IDS, YOUR_USERNAME,
    UPDATE_CHANNEL, A4F_API_URL, A4F_API_KEY, A4F_MODEL,
    MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, MAX_MEMORY, MAX_CPUS, MAX_PIDS,
    FREE_USER_LIMIT, SUBSCRIBED_USER_LIMIT, ADMIN_LIMIT, OWNER_LIMIT,
    BOT_START_TIME, RUNTIME_DIR, ALLOWED_EXTENSIONS, DOCKER_IMAGE, logger,
)
from database import (
    init_db, db_add_bot, db_get_bot, db_get_bot_any, db_get_user_bots,
    db_update_bot, db_delete_bot, db_save_env, db_get_envs, db_delete_all_envs,
    db_save_subscription, db_remove_subscription, db_load_subscriptions,
    db_add_active_user, db_load_active_users, db_add_admin, db_remove_admin,
    db_load_admins, db_pending_count, db_get_pending_bots, db_get_approved_stopped,
    db_count_approved, bots_col,
)
from r2_storage import r2_upload, r2_download, r2_delete
from security import encrypt_value, decrypt_value, generate_key
from env_manager import (
    inspect_zip_safety, safe_extract_zip, find_env_files, pick_primary_env,
    parse_env_file, encrypt_env_vars, decrypt_env_vars, get_env_keys,
    write_env_file, delete_env_file, find_entry, extract_zip_project,
)
from docker_manager import (
    make_container_name, docker_run, docker_stop, docker_exists, docker_logs,
)

# ---------------------------------------------------------------------------
# Pyrogram Client
# ---------------------------------------------------------------------------

app = Client(
    "hosting_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

bot_locked = False
user_subscriptions: dict[int, dict] = {}
user_files: dict[int, list[tuple[str, str]]] = {}
active_users: set[int] = set()
admin_ids: set[int] = set(ADMIN_IDS)
_user_notify_lock = threading.Lock()

# Text being awaited per user (for subscription, admin, env management)
awaiting_input: dict[int, str] = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_uptime() -> str:
    uptime = datetime.now() - BOT_START_TIME
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    return user_id in admin_ids


def get_user_file_limit(user_id: int) -> int | float:
    if user_id == OWNER_ID:
        return OWNER_LIMIT
    if user_id in admin_ids:
        return ADMIN_LIMIT
    sub = user_subscriptions.get(user_id)
    if sub and sub["expiry"] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT


def get_user_file_count(user_id: int) -> int:
    return len(user_files.get(user_id, []))


def save_user_file(user_id: int, filename: str, file_type: str) -> None:
    if user_id not in user_files:
        user_files[user_id] = []
    user_files[user_id] = [(fn, ft) for fn, ft in user_files[user_id] if fn != filename]
    user_files[user_id].append((filename, file_type))


def remove_user_file(user_id: int, filename: str) -> None:
    if user_id in user_files:
        user_files[user_id] = [(fn, ft) for fn, ft in user_files[user_id] if fn != filename]
        if not user_files[user_id]:
            del user_files[user_id]


def save_subscription(user_id: int, expiry: datetime) -> None:
    user_subscriptions[user_id] = {"expiry": expiry}
    db_save_subscription(user_id, expiry)


def remove_subscription(user_id: int) -> None:
    user_subscriptions.pop(user_id, None)
    db_remove_subscription(user_id)


def add_admin_db(admin_id: int) -> None:
    admin_ids.add(admin_id)
    db_add_admin(admin_id)


def remove_admin_db(admin_id: int) -> bool:
    if admin_id == OWNER_ID:
        return False
    admin_ids.discard(admin_id)
    db_remove_admin(admin_id)
    return True


# ---------------------------------------------------------------------------
# Notify helpers
# ---------------------------------------------------------------------------


async def _notify_user(user_id: int, text: str) -> None:
    try:
        await app.send_message(user_id, text)
    except Exception as e:
        logger.error("Notify user %d failed: %s", user_id, e)


def _notify_user_sync(user_id: int, text: str) -> None:
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            threading.Thread(target=lambda: asyncio.run(_notify_user(user_id, text))).start()
        else:
            loop.run_until_complete(_notify_user(user_id, text))
    except Exception:
        try:
            asyncio.run(_notify_user(user_id, text))
        except Exception as e:
            logger.error("Notify user %d failed: %s", user_id, e)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def main_menu_kb(user_id: int) -> types.InlineKeyboardMarkup:
    rows: list[list[types.InlineKeyboardButton]] = []

    if UPDATE_CHANNEL:
        rows.append(
            [types.InlineKeyboardButton("📢 Updates Channel", url=UPDATE_CHANNEL)]
        )

    rows.append(
        [
            types.InlineKeyboardButton("📤 Upload File", callback_data="upload", style=enums.ButtonStyle.PRIMARY),
            types.InlineKeyboardButton("📂 My Bots", callback_data="check_files", style=enums.ButtonStyle.SECONDARY),
        ]
    )
    rows.append(
        [
            types.InlineKeyboardButton("⚡ Bot Speed", callback_data="speed", style=enums.ButtonStyle.SECONDARY),
            types.InlineKeyboardButton("📊 Statistics", callback_data="stats", style=enums.ButtonStyle.SECONDARY),
        ]
    )

    if is_admin(user_id):
        pending = db_pending_count()
        ptxt = f"📋 Pending ({pending})" if pending else "📋 Pending Files"
        rows.append(
            [
                types.InlineKeyboardButton(ptxt, callback_data="view_pending", style=enums.ButtonStyle.PRIMARY),
                types.InlineKeyboardButton("💳 Subscriptions", callback_data="subscription", style=enums.ButtonStyle.SECONDARY),
            ]
        )
        rows.append(
            [
                types.InlineKeyboardButton("📢 Broadcast", callback_data="broadcast", style=enums.ButtonStyle.SECONDARY),
                types.InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel", style=enums.ButtonStyle.PRIMARY),
            ]
        )
        lock_text = "🔒 Lock Bot" if not bot_locked else "Unlock Bot"
        lock_data = "lock_bot" if not bot_locked else "unlock_bot"
        rows.append(
            [types.InlineKeyboardButton(lock_text, callback_data=lock_data, style=enums.ButtonStyle.DANGER)]
        )

    if YOUR_USERNAME:
        uname = YOUR_USERNAME.lstrip("@")
        rows.append(
            [types.InlineKeyboardButton("📞 Contact Owner", url=f"https://t.me/{uname}")]
        )

    rows.append(
        [types.InlineKeyboardButton("⏱ Uptime", callback_data="uptime", style=enums.ButtonStyle.SECONDARY)]
    )
    return types.InlineKeyboardMarkup(rows)


def bot_control_kb(bot_id: int, is_running: bool, approval: str) -> types.InlineKeyboardMarkup:
    aicon = "✅" if approval == "approved" else "⏳" if approval == "pending" else "❌"
    if is_running:
        return types.InlineKeyboardMarkup(
            [
                [
                    types.InlineKeyboardButton("🛑 Stop", callback_data=f"stop:{bot_id}", style=enums.ButtonStyle.DANGER),
                    types.InlineKeyboardButton("🔄 Restart", callback_data=f"restart:{bot_id}", style=enums.ButtonStyle.PRIMARY),
                ],
                [
                    types.InlineKeyboardButton("📜 Logs", callback_data=f"logs:{bot_id}", style=enums.ButtonStyle.SECONDARY),
                    types.InlineKeyboardButton("🔐 Environment", callback_data=f"env:{bot_id}", style=enums.ButtonStyle.SECONDARY),
                ],
                [
                    types.InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{bot_id}", style=enums.ButtonStyle.DANGER),
                ],
                [
                    types.InlineKeyboardButton(f"Status: {aicon} {approval.title()}", callback_data="noop", style=enums.ButtonStyle.SECONDARY),
                ],
            ]
        )
    return types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton("▶️ Start", callback_data=f"sbot:{bot_id}", style=enums.ButtonStyle.PRIMARY),
                types.InlineKeyboardButton("📜 Logs", callback_data=f"logs:{bot_id}", style=enums.ButtonStyle.SECONDARY),
            ],
            [
                types.InlineKeyboardButton("🔐 Environment", callback_data=f"env:{bot_id}", style=enums.ButtonStyle.SECONDARY),
                types.InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{bot_id}", style=enums.ButtonStyle.DANGER),
            ],
            [
                types.InlineKeyboardButton(f"Status: {aicon} {approval.title()}", callback_data="noop", style=enums.ButtonStyle.SECONDARY),
            ],
        ]
    )


def approval_kb(user_id: int, filename: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton("✅ Approve", callback_data=f"approve:{user_id}:{filename}", style=enums.ButtonStyle.PRIMARY),
                types.InlineKeyboardButton("❌ Reject", callback_data=f"reject:{user_id}:{filename}", style=enums.ButtonStyle.DANGER),
            ],
        ]
    )


def env_management_kb(bot_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton("➕ Add Variable", callback_data=f"env_add:{bot_id}", style=enums.ButtonStyle.PRIMARY),
                types.InlineKeyboardButton("✏️ Edit Variable", callback_data=f"env_edit:{bot_id}", style=enums.ButtonStyle.SECONDARY),
            ],
            [
                types.InlineKeyboardButton("🗑 Delete Variable", callback_data=f"env_del:{bot_id}", style=enums.ButtonStyle.DANGER),
            ],
            [
                types.InlineKeyboardButton("🔙 Back", callback_data=f"back_bot:{bot_id}", style=enums.ButtonStyle.SECONDARY),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Text builders
# ---------------------------------------------------------------------------


def _welcome_text(user_id: int, first_name: str, username: str | None) -> str:
    limit = get_user_file_limit(user_id)
    count = get_user_file_count(user_id)
    lim_str = "Unlimited" if limit == float("inf") else str(limit)
    if user_id == OWNER_ID:
        status = "Owner"
    elif user_id in admin_ids:
        status = "Admin"
    elif user_id in user_subscriptions:
        exp = user_subscriptions[user_id]["expiry"]
        if exp > datetime.now():
            status = "Premium"
            lim_str = str(SUBSCRIBED_USER_LIMIT)
        else:
            status = "Free (Expired)"
            remove_subscription(user_id)
    else:
        status = "Free User"
    return (
        f"🤖 **Welcome, {first_name}!**\n\n"
        f"🆔 ID: `{user_id}`\n"
        f"📛 @{username or 'N/A'}\n"
        f"📊 Status: **{status}**\n"
        f"📁 Files: {count} / {lim_str}\n\n"
        f"Upload `.py` or `.zip` files to host them in Docker.\n"
        f"All files require **admin approval** before running.\n\n"
        f"Use the buttons below."
    )


def _bot_summary(b: dict) -> str:
    icon = "🟢" if b["status"] == "running" else "🔴"
    aicon = "✅" if b["approval"] == "approved" else "⏳" if b["approval"] == "pending" else "❌"
    env_info = "🔐 Found" if b.get("env_file_found") else "🔐 Not found"
    return (
        f"📄 {b['filename']} ({b['file_type']})\n"
        f"🆔 ID: {b['id']}\n"
        f"💻 {icon} {b['status'].title()}\n"
        f"📝 Approval: {aicon} {b['approval'].title()}\n"
        f"{env_info}"
    )


# ---------------------------------------------------------------------------
# Stale cleanup
# ---------------------------------------------------------------------------


def cleanup_stale_bots() -> None:
    for row in bots_col.find({"status": "running"}, {"_id": 1, "container_name": 1}):
        cn = row.get("container_name")
        if cn and not docker_exists(cn):
            bots_col.update_one({"_id": row["_id"]}, {"$set": {"status": "stopped", "container_name": None, "runtime_path": None}})
            d = RUNTIME_DIR / str(row["_id"])
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            logger.info("Cleaned stale bot %d", row["_id"])


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------


def start_bot_docker(bot_id: int) -> str | None:
    b = db_get_bot_any(bot_id)
    if not b:
        return "Bot not found"
    if b["approval"] != "approved":
        return "Not approved yet"
    if b["status"] == "running":
        return "Already running"

    user_id = b["user_id"]

    try:
        data = r2_download(b["r2_key"])
    except Exception as e:
        return f"R2 download failed: {e}"

    work_dir = RUNTIME_DIR / str(bot_id)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    if b["file_type"] == "py":
        (work_dir / "bot.py").write_bytes(data)
    elif b["file_type"] == "zip":
        err = extract_zip_project(data, work_dir)
        if err:
            shutil.rmtree(work_dir, ignore_errors=True)
            return err

    env_file_path = work_dir / ".env"
    env_loaded = False
    if b.get("env_file_found"):
        env_loaded = write_env_file(bot_id, work_dir)
    if not env_loaded:
        env_file_path = None

    container_name = make_container_name(user_id, bot_id)
    try:
        docker_run(container_name, work_dir, env_file_path)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return f"Docker failed: {e}"

    db_update_bot(bot_id, status="running", container_name=container_name, runtime_path=str(work_dir))
    logger.info("Bot %d started, container=%s", bot_id, container_name)
    return None


def stop_bot_docker(bot_id: int) -> None:
    b = db_get_bot_any(bot_id)
    if not b:
        return
    cn = b.get("container_name")
    if cn and docker_exists(cn):
        try:
            docker_stop(cn)
        except Exception as e:
            logger.error("Docker stop bot=%d: %s", bot_id, e)
    d = RUNTIME_DIR / str(bot_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    db_update_bot(bot_id, status="stopped", container_name=None, runtime_path=None)
    logger.info("Bot %d stopped", bot_id)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, message: types.Message) -> None:
    uid = message.from_user.id
    if bot_locked and not is_admin(uid):
        await message.reply_text("🔒 Bot locked by admin. Try later.")
        return
    db_add_active_user(uid)
    active_users.add(uid)
    awaiting_input.pop(uid, None)
    await message.reply_text(
        _welcome_text(uid, message.from_user.first_name, message.from_user.username),
        reply_markup=main_menu_kb(uid),
    )


@app.on_message(filters.command("help") & filters.private)
async def cmd_help(client: Client, message: types.Message) -> None:
    awaiting_input.pop(message.from_user.id, None)
    await message.reply_text(
        "📖 **Commands**\n\n"
        "/start - Main menu\n"
        "/bots - List your bots\n"
        "/env <bot_id> - View environment variables\n"
        "/help - This help\n\n"
        "Upload `.py` or `.zip` to host.\n"
        "All files need admin approval.",
    )


@app.on_message(filters.command("bots") & filters.private)
async def cmd_bots(client: Client, message: types.Message) -> None:
    uid = message.from_user.id
    bots = db_get_user_bots(uid)
    if not bots:
        await message.reply_text("No bots yet. Upload a file to get started.")
        return
    for b in bots:
        running = b["status"] == "running"
        await message.reply_text(
            f"🤖 **Your Bot**\n\n{_bot_summary(b)}",
            reply_markup=bot_control_kb(b["id"], running, b["approval"]),
        )


@app.on_message(filters.command("env") & filters.private)
async def cmd_env(client: Client, message: types.Message) -> None:
    uid = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply_text("Usage: `/env <bot_id>`")
        return
    try:
        bot_id = int(parts[1])
    except ValueError:
        await message.reply_text("Invalid bot ID.")
        return
    b = db_get_bot(bot_id, uid)
    if not b:
        b_any = db_get_bot_any(bot_id)
        if not b_any or not is_admin(uid):
            await message.reply_text("❌ You don't have permission to access this bot.")
            return
        b = b_any
    keys = get_env_keys(bot_id)
    if not keys:
        await message.reply_text("🔐 No environment variables stored for this bot.")
        return
    masked = "\n".join(f"`{k}` = ********" for k in keys)
    await message.reply_text(
        f"🔐 **Environment Variables** (Bot `{bot_id}`)\n\n{masked}",
        reply_markup=env_management_kb(bot_id),
    )


@app.on_message(filters.command("ping") & filters.private)
async def cmd_ping(client: Client, message: types.Message) -> None:
    start_t = time.time()
    msg = await message.reply_text("Pong!")
    lat = round((time.time() - start_t) * 1000, 2)
    await msg.edit_text(f"Pong!\nLatency: {lat} ms\nUptime: {get_uptime()}")


@app.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(client: Client, message: types.Message) -> None:
    awaiting_input.pop(message.from_user.id, None)
    await message.reply_text("Cancelled.")


@app.on_message(filters.command("mpx") & filters.private)
async def cmd_mpx(client: Client, message: types.Message) -> None:
    uid = message.from_user.id
    if bot_locked and not is_admin(uid):
        await message.reply_text("Bot locked.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: `/mpx Your question here`")
        return
    query = parts[1]
    if not A4F_API_URL or not A4F_API_KEY:
        await message.reply_text("AI API not configured.")
        return
    try:
        resp = requests.post(
            A4F_API_URL,
            headers={"Authorization": f"Bearer {A4F_API_KEY}", "Content-Type": "application/json"},
            json={"model": A4F_MODEL, "messages": [{"role": "user", "content": query}], "temperature": 0.7},
            timeout=30,
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"]
        for i in range(0, len(answer), 4000):
            await message.reply_text(answer[i : i + 4000])
    except Exception as e:
        logger.error("MPX error: %s", e)
        await message.reply_text("AI request failed.")


@app.on_message(filters.command("pending") & filters.private)
async def cmd_pending(client: Client, message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        await message.reply_text("Admin only.")
        return
    bots = db_get_pending_bots()
    if not bots:
        await message.reply_text("No pending files.")
        return
    text = "📋 **Pending Files**\n\n"
    kb_rows: list[list[types.InlineKeyboardButton]] = []
    for b in bots[:20]:
        text += f"• `{b['filename']}` (User: `{b['user_id']}`)\n"
        kb_rows.append(
            [
                types.InlineKeyboardButton(
                    f"👤 {b['user_id']} | 📁 {b['filename']}",
                    callback_data=f"review:{b['id']}",
                    style=enums.ButtonStyle.SECONDARY,
                )
            ]
        )
    kb_rows.append(
        [types.InlineKeyboardButton("🔙 Back", callback_data="back_main", style=enums.ButtonStyle.SECONDARY)]
    )
    await message.reply_text(text, reply_markup=types.InlineKeyboardMarkup(kb_rows))


# ---------------------------------------------------------------------------
# Document handler
# ---------------------------------------------------------------------------


@app.on_message(filters.document & filters.private)
async def handle_document(client: Client, message: types.Message) -> None:
    uid = message.from_user.id
    doc = message.document
    filename = doc.file_name or ""
    ext = Path(filename).suffix.lower()

    if bot_locked and not is_admin(uid):
        await message.reply_text("Bot locked.")
        return

    if ext not in ALLOWED_EXTENSIONS:
        await message.reply_text(f"❌ Unsupported: {ext}\nAllowed: .py, .zip")
        return

    fsize = doc.file_size or 0
    if fsize > MAX_UPLOAD_BYTES:
        await message.reply_text(f"❌ Too large ({fsize // (1024*1024)} MB). Max: {MAX_UPLOAD_MB} MB")
        return

    limit = get_user_file_limit(uid)
    count = get_user_file_count(uid)
    if count >= limit:
        lim_str = "Unlimited" if limit == float("inf") else str(limit)
        await message.reply_text(f"❌ File limit reached ({count}/{lim_str}).")
        return

    status_msg = await message.reply_text("📥 Downloading...")

    try:
        data = await client.download_media(message, in_memory=True)
    except Exception as e:
        logger.error("Download failed user=%s: %s", uid, e)
        await status_msg.edit_text("❌ Download failed.")
        return

    # ZIP inspection
    env_found = False
    env_count = 0
    env_keys_list: list[str] = []

    if ext == ".zip":
        safe, err = inspect_zip_safety(data)
        if not safe:
            await status_msg.edit_text(f"❌ Unsafe ZIP file\n\n{err}")
            return

        env_files = find_env_files(data)
        if env_files:
            primary = pick_primary_env(env_files)
            if primary:
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        content = zf.read(primary).decode("utf-8", errors="replace")
                        env_vars = parse_env_file(content)
                        if env_vars:
                            env_found = True
                            env_count = encrypt_env_vars(0, env_vars)  # temp bot_id=0, will update after insert
                            env_keys_list = list(env_vars.keys())
                except Exception as e:
                    logger.error("ZIP env parse error user=%s: %s", uid, e)

    await status_msg.edit_text("☁️ Uploading to R2...")

    file_type = "py" if ext == ".py" else "zip"
    bot_id = db_add_bot(uid, filename, file_type, r2_key="", env_found=env_found)

    # Re-encrypt with correct bot_id and upload to R2 with bot_id
    try:
        r2_key = r2_upload(uid, bot_id, filename, data)
        db_update_bot(bot_id, r2_key=r2_key)
    except Exception as e:
        logger.error("R2 upload failed user=%s: %s", uid, e)
        await status_msg.edit_text("❌ Cloud storage upload failed.")
        return

    # Re-encrypt env vars with correct bot_id if found
    if env_found and env_keys_list:
        db_delete_all_envs(0)  # remove temp entries
        # Re-parse and encrypt with correct bot_id
        if ext == ".zip":
            try:
                env_files = find_env_files(data)
                primary = pick_primary_env(env_files)
                if primary:
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        content = zf.read(primary).decode("utf-8", errors="replace")
                        env_vars = parse_env_file(content)
                        if env_vars:
                            encrypt_env_vars(bot_id, env_vars)
                            env_count = len(env_vars)
            except Exception:
                pass

    save_user_file(uid, filename, file_type)
    logger.info("Uploaded user=%s bot=%d file=%s", uid, bot_id, filename)

    env_text = f"🔐 Environment file detected\n🔑 Variables found: {env_count}" if env_found else "🔐 Environment file: Not found"
    text = (
        f"✅ **Uploaded!**\n\n"
        f"📦 Project: `{filename}`\n"
        f"🆔 Bot ID: `{bot_id}`\n"
        f"☁️ Stored in Cloudflare R2\n\n"
        f"{env_text}\n\n"
        f"📝 Approval: ⏳ **Pending**\n\n"
        f"Admins have been notified."
    )
    await status_msg.edit_text(text, reply_markup=bot_control_kb(bot_id, False, "pending"))

    for aid in admin_ids:
        try:
            await app.send_message(
                aid,
                f"📄 **New File for Approval**\n\n"
                f"👤 User: `{uid}`\n"
                f"📛 File: `{filename}`\n"
                f"📊 Type: {file_type}\n"
                f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                reply_markup=approval_kb(uid, filename),
            )
        except Exception as e:
            logger.error("Notify admin %d failed: %s", aid, e)


# ---------------------------------------------------------------------------
# Text handler (awaiting inputs)
# ---------------------------------------------------------------------------


@app.on_message(filters.private & ~filters.command(["start", "help", "bots", "env", "mpx", "pending", "ping", "cancel"]))
async def handle_text(client: Client, message: types.Message) -> None:
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        return

    # Handle awaiting state
    state = awaiting_input.get(uid)

    if state == "add_sub":
        awaiting_input.pop(uid, None)
        if not is_admin(uid):
            return
        parts = text.split()
        if len(parts) == 2:
            try:
                target_id = int(parts[0])
                days = int(parts[1])
                if target_id > 0 and days > 0:
                    current = user_subscriptions.get(target_id, {}).get("expiry")
                    start = datetime.now()
                    if current and current > start:
                        start = current
                    new_exp = start + timedelta(days=days)
                    save_subscription(target_id, new_exp)
                    await message.reply_text(f"✅ Sub added for `{target_id}`: {days} days\nExpires: {new_exp:%Y-%m-%d}")
                    try:
                        await _notify_user(target_id, f"✅ Subscription activated! Expires: {new_exp:%Y-%m-%d}")
                    except Exception:
                        pass
                    return
            except ValueError:
                pass
        await message.reply_text("Invalid format. Use: `USER_ID DAYS`")
        return

    if state == "rm_sub":
        awaiting_input.pop(uid, None)
        if not is_admin(uid):
            return
        try:
            target_id = int(text)
            remove_subscription(target_id)
            await message.reply_text(f"✅ Sub removed for `{target_id}`")
        except ValueError:
            await message.reply_text("Invalid user ID.")
        return

    if state == "check_sub":
        awaiting_input.pop(uid, None)
        if not is_admin(uid):
            return
        try:
            target_id = int(text)
            if target_id in user_subscriptions:
                exp = user_subscriptions[target_id]["expiry"]
                if exp > datetime.now():
                    left = (exp - datetime.now()).days
                    await message.reply_text(f"User `{target_id}`: expires {exp:%Y-%m-%d} ({left}d left)")
                else:
                    await message.reply_text(f"User `{target_id}`: expired")
                    remove_subscription(target_id)
            else:
                await message.reply_text(f"User `{target_id}`: no sub")
        except ValueError:
            await message.reply_text("Invalid user ID.")
        return

    if state == "add_admin":
        awaiting_input.pop(uid, None)
        if not is_owner(uid):
            return
        try:
            target_id = int(text)
            add_admin_db(target_id)
            await message.reply_text(f"✅ `{target_id}` promoted to admin.")
        except ValueError:
            await message.reply_text("Invalid user ID.")
        return

    if state == "rm_admin":
        awaiting_input.pop(uid, None)
        if not is_owner(uid):
            return
        try:
            target_id = int(text)
            if remove_admin_db(target_id):
                await message.reply_text(f"✅ `{target_id}` demoted.")
            else:
                await message.reply_text("Cannot remove the owner.")
        except ValueError:
            await message.reply_text("Invalid user ID.")
        return

    if state and state.startswith("env_add:"):
        awaiting_input.pop(uid, None)
        bot_id = int(state.split(":")[1])
        b = db_get_bot(bot_id, uid)
        if not b:
            b_any = db_get_bot_any(bot_id)
            if not b_any or not is_admin(uid):
                await message.reply_text("Access denied.")
                return
            b = b_any
        parts = text.split("=", 1)
        if len(parts) != 2:
            await message.reply_text("Format: `KEY=VALUE`")
            return
        key = parts[0].strip()
        value = parts[1].strip()
        encrypted = encrypt_value(value)
        db_save_env(bot_id, key, encrypted)
        await message.reply_text(f"✅ Variable `{key}` added to bot `{bot_id}`.")
        return

    if state and state.startswith("env_del:"):
        awaiting_input.pop(uid, None)
        bot_id = int(state.split(":")[1])
        b = db_get_bot(bot_id, uid)
        if not b:
            b_any = db_get_bot_any(bot_id)
            if not b_any or not is_admin(uid):
                await message.reply_text("Access denied.")
                return
            b = b_any
        key = text.strip()
        from database import db_delete_env
        db_delete_env(bot_id, key)
        await message.reply_text(f"✅ Variable `{key}` deleted from bot `{bot_id}`.")
        return

    if state and state.startswith("env_edit:"):
        bot_id = int(state.split(":")[1])
        b = db_get_bot(bot_id, uid)
        if not b:
            b_any = db_get_bot_any(bot_id)
            if not b_any or not is_admin(uid):
                awaiting_input.pop(uid, None)
                await message.reply_text("Access denied.")
                return
            b = b_any
        key = text.strip()
        keys = get_env_keys(bot_id)
        if key not in keys:
            awaiting_input.pop(uid, None)
            await message.reply_text(f"❌ Key `{key}` not found.\n\nAvailable keys: {', '.join(keys)}")
            return
        awaiting_input[uid] = f"env_edit_val:{bot_id}:{key}"
        await message.reply_text(f"Send the new value for `{key}`.\n/cancel to abort.")
        return

    if state and state.startswith("env_edit_val:"):
        awaiting_input.pop(uid, None)
        parts_state = state.split(":", 2)
        bot_id = int(parts_state[1])
        key = parts_state[2]
        b = db_get_bot(bot_id, uid)
        if not b:
            b_any = db_get_bot_any(bot_id)
            if not b_any or not is_admin(uid):
                await message.reply_text("Access denied.")
                return
            b = b_any
        encrypted = encrypt_value(text)
        db_save_env(bot_id, key, encrypted)
        await message.reply_text(f"✅ Variable `{key}` updated for bot `{bot_id}`.")
        return

    # Broadcast handler
    if uid in admin_ids and message.reply_to_message and "broadcast" in (message.reply_to_message.text or "").lower():
        users = list(active_users)
        sent, failed = 0, 0
        for target in users:
            try:
                await app.send_message(target, text)
                sent += 1
            except Exception:
                failed += 1
            if (sent + failed) % 25 == 0:
                time.sleep(1)
        await message.reply_text(f"📢 Broadcast done!\nSent: {sent}\nFailed: {failed}")
        return


# ---------------------------------------------------------------------------
# Callback router
# ---------------------------------------------------------------------------


@app.on_callback_query(filters.regex(r"^(approve|reject|review|sbot|stop|restart|delete|logs|noop|env|env_add|env_del|env_edit|back_bot):"))
async def cb_router(client: Client, cb: types.CallbackQuery) -> None:
    uid = cb.from_user.id
    parts = cb.data.split(":", 2)
    action = parts[0]

    if action == "noop":
        await cb.answer()
        return

    if action in ("approve", "reject"):
        await _cb_approval(client, cb, uid, parts)
    elif action == "review":
        await _cb_review(client, cb, uid, parts)
    elif action == "sbot":
        await _cb_start(client, cb, uid, int(parts[1]))
    elif action == "stop":
        await _cb_stop(client, cb, uid, int(parts[1]))
    elif action == "restart":
        await _cb_restart(client, cb, uid, int(parts[1]))
    elif action == "delete":
        await _cb_delete(client, cb, uid, int(parts[1]))
    elif action == "logs":
        await _cb_logs(client, cb, uid, int(parts[1]))
    elif action == "env":
        await _cb_env(client, cb, uid, int(parts[1]))
    elif action == "env_add":
        awaiting_input[uid] = f"env_add:{parts[1]}"
        await cb.answer()
        await cb.message.reply_text("Send the variable in format: `KEY=VALUE`\n/cancel to abort.")
    elif action == "env_del":
        awaiting_input[uid] = f"env_del:{parts[1]}"
        await cb.answer()
        await cb.message.reply_text("Send the variable key to delete.\n/cancel to abort.")
    elif action == "env_edit":
        awaiting_input[uid] = f"env_edit:{parts[1]}"
        await cb.answer()
        await cb.message.reply_text("Send the variable key to edit.\n/cancel to abort.")
    elif action == "back_bot":
        bot_id = int(parts[1])
        b = db_get_bot(bot_id, uid)
        if not b:
            b_any = db_get_bot_any(bot_id)
            if not b_any or not is_admin(uid):
                await cb.answer("Not found.", show_alert=True)
                return
            b = b_any
        await cb.answer()
        await cb.message.edit_text(
            f"🤖 **Bot**\n\n{_bot_summary(b)}",
            reply_markup=bot_control_kb(b["id"], b["status"] == "running", b["approval"]),
        )


@app.on_callback_query(filters.regex(r"^(upload|check_files|speed|stats|back_main|uptime|lock_bot|unlock_bot|subscription|broadcast|admin_panel|view_pending|run_all)$"))
async def cb_menu(client: Client, cb: types.CallbackQuery) -> None:
    global bot_locked
    uid = cb.from_user.id
    action = cb.data

    if action == "upload":
        limit = get_user_file_limit(uid)
        count = get_user_file_count(uid)
        if count >= limit:
            lim = "Unlimited" if limit == float("inf") else str(limit)
            await cb.answer(f"Limit reached ({count}/{lim})", show_alert=True)
            return
        await cb.answer()
        await cb.message.reply_text("Send your `.py` or `.zip` file.\n\n⚠️ **Admin approval required.**")

    elif action == "check_files":
        bots = db_get_user_bots(uid)
        if not bots:
            await cb.answer("No bots.", show_alert=True)
            return
        await cb.answer()
        text = "📂 **Your Bots**\n\n"
        kb_rows: list[list[types.InlineKeyboardButton]] = []
        for b in bots:
            icon = "🟢" if b["status"] == "running" else "🔴"
            aicon = "✅" if b["approval"] == "approved" else "⏳" if b["approval"] == "pending" else "❌"
            text += f"{icon} `{b['filename']}` ({b['file_type']}) {aicon}\n"
            cb_data = f"sbot:{b['id']}" if b["approval"] == "approved" else "noop"
            kb_rows.append(
                [
                    types.InlineKeyboardButton(
                        f"{aicon} {b['filename']}",
                        callback_data=cb_data,
                        style=enums.ButtonStyle.PRIMARY if b["status"] == "running" else enums.ButtonStyle.SECONDARY,
                    )
                ]
            )
        kb_rows.append(
            [types.InlineKeyboardButton("🔙 Back", callback_data="back_main", style=enums.ButtonStyle.SECONDARY)]
        )
        await cb.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(kb_rows))

    elif action == "speed":
        t0 = time.time()
        lat = round((time.time() - t0) * 1000, 2)
        status = "Locked" if bot_locked else "Unlocked"
        level = "Owner" if uid == OWNER_ID else "Admin" if uid in admin_ids else "Premium" if uid in user_subscriptions else "Free"
        msg = f"⚡ **Bot Speed**\n\nAPI: {lat} ms\nStatus: {status}\nLevel: {level}"
        if is_admin(uid):
            msg += f"\n📋 Pending: {db_pending_count()}"
        await cb.answer()
        await cb.message.edit_text(msg, reply_markup=main_menu_kb(uid))

    elif action == "stats":
        total = len(active_users)
        total_files = sum(len(v) for v in user_files.values())
        running = sum(1 for b in db_get_user_bots(uid) if b["status"] == "running")
        msg = (
            f"📊 **Statistics**\n\n"
            f"Total Users: {total}\n"
            f"Total Files: {total_files}\n"
        )
        if is_admin(uid):
            approved = db_count_approved()
            msg += (
                f"✅ Approved: {approved}\n"
                f"⏳ Pending: {db_pending_count()}\n"
                f"🔒 Locked: {'Yes' if bot_locked else 'No'}\n"
            )
        msg += f"Your Running: {running}"
        await cb.answer()
        await cb.message.edit_text(msg, reply_markup=main_menu_kb(uid))

    elif action == "back_main":
        await cb.answer()
        await cb.message.edit_text(
            _welcome_text(uid, cb.from_user.first_name, cb.from_user.username),
            reply_markup=main_menu_kb(uid),
        )

    elif action == "uptime":
        await cb.answer(f"Uptime: {get_uptime()}", show_alert=True)

    elif action == "lock_bot":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        bot_locked = True
        logger.warning("Bot locked by %d", uid)
        await cb.answer("Locked")
        await cb.message.edit_reply_markup(main_menu_kb(uid))

    elif action == "unlock_bot":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        bot_locked = False
        logger.warning("Bot unlocked by %d", uid)
        await cb.answer("Unlocked")
        await cb.message.edit_reply_markup(main_menu_kb(uid))

    elif action == "subscription":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        await cb.answer()
        await cb.message.edit_text(
            "💳 **Subscription Management**",
            reply_markup=types.InlineKeyboardMarkup(
                [
                    [
                        types.InlineKeyboardButton("➕ Add Sub", callback_data="add_sub", style=enums.ButtonStyle.PRIMARY),
                        types.InlineKeyboardButton("➖ Remove Sub", callback_data="rm_sub", style=enums.ButtonStyle.DANGER),
                    ],
                    [
                        types.InlineKeyboardButton("🔍 Check Sub", callback_data="check_sub", style=enums.ButtonStyle.SECONDARY),
                    ],
                    [types.InlineKeyboardButton("🔙 Back", callback_data="back_main", style=enums.ButtonStyle.SECONDARY)],
                ]
            ),
        )

    elif action == "broadcast":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        await cb.answer()
        await cb.message.reply_text("📢 Send the message to broadcast.\n/cancel to abort.")

    elif action == "admin_panel":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        await cb.answer()
        await cb.message.edit_text(
            "👑 **Admin Panel**",
            reply_markup=types.InlineKeyboardMarkup(
                [
                    [
                        types.InlineKeyboardButton("➕ Add Admin", callback_data="add_admin_init", style=enums.ButtonStyle.PRIMARY),
                        types.InlineKeyboardButton("➖ Remove Admin", callback_data="rm_admin_init", style=enums.ButtonStyle.DANGER),
                    ],
                    [
                        types.InlineKeyboardButton("📋 List Admins", callback_data="list_admins", style=enums.ButtonStyle.SECONDARY),
                        types.InlineKeyboardButton("📋 Pending", callback_data="view_pending", style=enums.ButtonStyle.SECONDARY),
                    ],
                    [types.InlineKeyboardButton("🔙 Back", callback_data="back_main", style=enums.ButtonStyle.SECONDARY)],
                ]
            ),
        )

    elif action == "view_pending":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        bots = db_get_pending_bots()
        if not bots:
            await cb.answer("No pending files.", show_alert=True)
            return
        await cb.answer()
        kb_rows: list[list[types.InlineKeyboardButton]] = []
        for b in bots[:20]:
            kb_rows.append(
                [
                    types.InlineKeyboardButton(
                        f"👤 {b['user_id']} | 📁 {b['filename']}",
                        callback_data=f"review:{b['id']}",
                        style=enums.ButtonStyle.SECONDARY,
                    )
                ]
            )
        kb_rows.append(
            [types.InlineKeyboardButton("🔙 Back", callback_data="back_main", style=enums.ButtonStyle.SECONDARY)]
        )
        await cb.message.edit_text(
            f"📋 **Pending Files** ({len(bots)})",
            reply_markup=types.InlineKeyboardMarkup(kb_rows),
        )

    elif action == "run_all":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        await cb.answer("Starting all...")
        await cb.message.reply_text("Starting all approved bots...")
        count = 0
        for row in db_get_approved_stopped():
            err = start_bot_docker(row["id"])
            if err is None:
                count += 1
            time.sleep(0.5)
        await cb.message.reply_text(f"Started {count} bots.")


# ---------------------------------------------------------------------------
# Subscription/Admin sub-callbacks
# ---------------------------------------------------------------------------


@app.on_callback_query(filters.regex(r"^(add_sub|rm_sub|check_sub|add_admin_init|rm_admin_init|list_admins)$"))
async def cb_sub_callbacks(client: Client, cb: types.CallbackQuery) -> None:
    uid = cb.from_user.id
    action = cb.data

    if action == "add_sub":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        awaiting_input[uid] = "add_sub"
        await cb.answer()
        await cb.message.reply_text("Send: `USER_ID DAYS`\nExample: `12345678 30`\n/cancel to abort.")

    elif action == "rm_sub":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        awaiting_input[uid] = "rm_sub"
        await cb.answer()
        await cb.message.reply_text("Send User ID to remove sub.\n/cancel to abort.")

    elif action == "check_sub":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        awaiting_input[uid] = "check_sub"
        await cb.answer()
        await cb.message.reply_text("Send User ID to check.\n/cancel to abort.")

    elif action == "add_admin_init":
        if not is_owner(uid):
            await cb.answer("Owner only.", show_alert=True)
            return
        awaiting_input[uid] = "add_admin"
        await cb.answer()
        await cb.message.reply_text("Send User ID to promote.\n/cancel to abort.")

    elif action == "rm_admin_init":
        if not is_owner(uid):
            await cb.answer("Owner only.", show_alert=True)
            return
        awaiting_input[uid] = "rm_admin"
        await cb.answer()
        await cb.message.reply_text("Send User ID to demote.\n/cancel to abort.")

    elif action == "list_admins":
        if not is_admin(uid):
            await cb.answer("Admin only.", show_alert=True)
            return
        await cb.answer()
        lines = [f"• `{a}` {'👑' if a == OWNER_ID else ''}" for a in sorted(admin_ids)]
        await cb.message.edit_text(
            f"👑 **Admins**\n\n" + "\n".join(lines),
            reply_markup=types.InlineKeyboardMarkup(
                [[types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel", style=enums.ButtonStyle.SECONDARY)]]
            ),
        )


# ---------------------------------------------------------------------------
# Callback implementations
# ---------------------------------------------------------------------------


async def _cb_approval(client: Client, cb: types.CallbackQuery, uid: int, parts: list[str]) -> None:
    if not is_admin(uid):
        await cb.answer("Admin only.", show_alert=True)
        return
    action = parts[0]
    target_user = int(parts[1])
    filename = parts[2]

    approval = "approved" if action == "approve" else "rejected"
    row = bots_col.find_one(
        {"user_id": target_user, "filename": filename, "approval": "pending"},
        {"_id": 1},
    )
    if row:
        db_update_bot(row["_id"], approval=approval)

    icon = "✅" if approval == "approved" else "❌"
    try:
        await _notify_user(
            target_user,
            f"{icon} **File {approval.title()}**\n\n📁 `{filename}`\n👮 By: Admin `{uid}`",
        )
    except Exception:
        pass

    await cb.answer(f"{icon} {approval}")
    try:
        await cb.message.edit_text(
            f"{icon} **{approval.upper()}**\n\n📁 `{filename}`\n👤 `{target_user}`\n👮 `{uid}`",
        )
    except Exception:
        pass


async def _cb_review(client: Client, cb: types.CallbackQuery, uid: int, parts: list[str]) -> None:
    if not is_admin(uid):
        await cb.answer("Admin only.", show_alert=True)
        return
    bot_id = int(parts[1])
    b = db_get_bot_any(bot_id)
    if not b:
        await cb.answer("Not found.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"📋 **Review**\n\n"
        f"🆔 Bot: `{b['id']}`\n"
        f"👤 User: `{b['user_id']}`\n"
        f"📁 File: `{b['filename']}`\n"
        f"📊 Type: {b['file_type']}\n"
        f"🔐 Env: {'Found' if b.get('env_file_found') else 'Not found'}\n"
        f"📝 Status: {b['approval'].title()}",
        reply_markup=approval_kb(b["user_id"], b["filename"]),
    )


async def _cb_start(client: Client, cb: types.CallbackQuery, uid: int, bot_id: int) -> None:
    b = db_get_bot(bot_id, uid)
    if not b:
        b_any = db_get_bot_any(bot_id)
        if not b_any:
            await cb.answer("Not found.", show_alert=True)
            return
        if not is_admin(uid):
            await cb.answer("Not your bot.", show_alert=True)
            return
        b = b_any

    if b["status"] == "running":
        await cb.answer("Already running.", show_alert=True)
        return

    if b["approval"] != "approved":
        await cb.answer(f"Not approved ({b['approval']}).", show_alert=True)
        return

    await cb.answer("Starting...")
    await cb.message.edit_text("🐳 Starting Docker container...")

    err = start_bot_docker(bot_id)
    if err:
        await cb.message.edit_text(f"❌ {err}", reply_markup=bot_control_kb(bot_id, False, b["approval"]))
        return

    env_status = "🔐 Environment: Loaded" if b.get("env_file_found") else "🔐 Environment: None"
    await cb.message.edit_text(
        f"🟢 **Bot Started**\n\n"
        f"📦 `{b['filename']}`\n"
        f"🆔 Bot ID: `{bot_id}`\n"
        f"💻 VPS: Running\n"
        f"{env_status}\n"
        f"☁️ Source: Cloudflare R2",
        reply_markup=bot_control_kb(bot_id, True, "approved"),
    )


async def _cb_stop(client: Client, cb: types.CallbackQuery, uid: int, bot_id: int) -> None:
    b = db_get_bot(bot_id, uid)
    if not b:
        b_any = db_get_bot_any(bot_id)
        if not b_any or not is_admin(uid):
            await cb.answer("Not found.", show_alert=True)
            return
        b = b_any
    if b["status"] != "running":
        await cb.answer("Not running.", show_alert=True)
        return

    await cb.answer("Stopping...")
    stop_bot_docker(bot_id)
    await cb.message.edit_text(
        f"🔴 **Bot Stopped**\n\n"
        f"🆔 Bot ID: `{bot_id}`\n\n"
        f"Docker container removed.\n"
        f"VPS runtime files deleted.\n"
        f".env deleted from VPS.\n\n"
        f"☁️ Original project remains in Cloudflare R2.\n"
        f"🔐 Environment variables remain securely stored for the next start.",
        reply_markup=bot_control_kb(bot_id, False, b["approval"]),
    )


async def _cb_restart(client: Client, cb: types.CallbackQuery, uid: int, bot_id: int) -> None:
    b = db_get_bot(bot_id, uid)
    if not b:
        b_any = db_get_bot_any(bot_id)
        if not b_any or not is_admin(uid):
            await cb.answer("Not found.", show_alert=True)
            return
        b = b_any

    if b["approval"] != "approved":
        await cb.answer("Not approved.", show_alert=True)
        return

    await cb.answer("Restarting...")
    stop_bot_docker(bot_id)
    await cb.message.edit_text("🐳 Restarting...")
    err = start_bot_docker(bot_id)
    if err:
        await cb.message.edit_text(f"❌ {err}", reply_markup=bot_control_kb(bot_id, False, b["approval"]))
        return
    await cb.message.edit_text(
        f"🟢 **Restarted**\n\n📦 `{b['filename']}`",
        reply_markup=bot_control_kb(bot_id, True, "approved"),
    )


async def _cb_delete(client: Client, cb: types.CallbackQuery, uid: int, bot_id: int) -> None:
    b = db_get_bot(bot_id, uid)
    if not b:
        b_any = db_get_bot_any(bot_id)
        if not b_any or not is_admin(uid):
            await cb.answer("Not found.", show_alert=True)
            return
        b = b_any

    if b["status"] == "running":
        await cb.answer("Stop the bot before deleting it.", show_alert=True)
        return

    await cb.answer("Deleting...")
    if b["status"] == "running":
        stop_bot_docker(bot_id)

    try:
        r2_delete(b["r2_key"])
    except Exception as e:
        logger.error("R2 delete bot=%d: %s", bot_id, e)

    db_delete_all_envs(bot_id)

    d = RUNTIME_DIR / str(bot_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)

    db_delete_bot(bot_id)
    remove_user_file(b["user_id"], b["filename"])

    await cb.message.edit_text(
        f"🗑 **Bot Deleted**\n\n"
        f"The project was deleted from:\n"
        f"☁️ Cloudflare R2\n"
        f"🗄️ Database\n\n"
        f"Any VPS runtime files were also removed."
    )


async def _cb_logs(client: Client, cb: types.CallbackQuery, uid: int, bot_id: int) -> None:
    b = db_get_bot(bot_id, uid)
    if not b:
        b_any = db_get_bot_any(bot_id)
        if not b_any or not is_admin(uid):
            await cb.answer("Not found.", show_alert=True)
            return
        b = b_any

    cn = b.get("container_name")
    if not cn or not docker_exists(cn):
        await cb.answer("No container.", show_alert=True)
        return

    logs = docker_logs(cn, tail=40)
    if not logs.strip():
        logs = "(empty)"

    await cb.answer()
    await cb.message.reply_text(
        f"📜 **Logs** `{b['filename']}`\n\n```\n{logs[-3800:]}\n```"
    )


async def _cb_env(client: Client, cb: types.CallbackQuery, uid: int, bot_id: int) -> None:
    b = db_get_bot(bot_id, uid)
    if not b:
        b_any = db_get_bot_any(bot_id)
        if not b_any or not is_admin(uid):
            await cb.answer("Not found.", show_alert=True)
            return
        b = b_any

    keys = get_env_keys(bot_id)
    if not keys:
        await cb.answer("No environment variables.", show_alert=True)
        return

    masked = "\n".join(f"`{k}` = ********" for k in keys)
    await cb.answer()
    await cb.message.reply_text(
        f"🔐 **Environment Variables** (Bot `{bot_id}`)\n\n{masked}",
        reply_markup=env_management_kb(bot_id),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    init_db()
    global user_subscriptions, active_users, admin_ids
    user_subscriptions = db_load_subscriptions()
    active_users = db_load_active_users()
    admin_ids = db_load_admins()
    cleanup_stale_bots()
    logger.info("Hosting bot starting...")
    app.run()


if __name__ == "__main__":
    main()
