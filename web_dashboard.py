"""Web Dashboard - Flask web server for the HostBot platform.

Runs on the VPS alongside the Telegram bot (both share MongoDB + Docker).
Users sign in with email/password, or via Firebase (Google) or GitHub OAuth.
They can upload files or clone a git repo, then manage/run their bots.

Run:
    python web_dashboard.py         # serves http://0.0.0.0:9090 (DASHBOARD_PORT)
"""

import os
import re
import io
import secrets
import zipfile
import shutil
import threading
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, request, send_from_directory, Response, redirect
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import generate_password_hash, check_password_hash

import database
from config import (
    ENV_ENCRYPTION_KEY, RUNTIME_DIR, WEB_URL, ADMIN_USERNAME, ADMIN_PASSWORD,
    OWNER_ID, logger, FIREBASE_CREDENTIALS, FIREBASE_DATABASE_URL,
    FIREBASE_PROJECT_ID, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET,
    GITHUB_REDIRECT_URI, FIREBASE_WEB_API_KEY, FIREBASE_AUTH_DOMAIN,
    FIREBASE_STORAGE_BUCKET, FIREBASE_MESSAGING_SENDER_ID, FIREBASE_WEB_APP_ID,
)
from docker_manager import docker_exists, docker_logs
from env_manager import get_env_keys, decrypt_env_vars
from security import encrypt_value
from r2_storage import r2_delete, r2_download, r2_upload

WEB_DIR = Path(__file__).parent / "web"

# ---------------------------------------------------------------- auth

# Signed token secret derived from the existing encryption key (never hard-coded).
_TTL_PERSIST = 60 * 60 * 24 * 7       # "Remember me" sessions last 7 days
_TTL_SESSION = 60 * 60 * 24          # session-only login expires after 1 day
_serializer = URLSafeTimedSerializer(ENV_ENCRYPTION_KEY or "hostbot-dashboard-secret")


def _remember_from(body_or_args) -> bool:
    remember = True
    if isinstance(body_or_args, dict):
        remember = bool(body_or_args.get("remember", True))
    return remember


def _make_token(user_id: int, remember: bool = True, **extra) -> str:
    payload = {"uid": user_id, "remember": remember}
    payload.update(extra)
    # Embed the expiry so the browser can enforce it client-side too.
    ttl = _TTL_PERSIST if remember else _TTL_SESSION
    payload["exp"] = int((datetime.utcnow() + timedelta(seconds=ttl)).timestamp())
    return _serializer.dumps(payload)


def _read_token() -> dict | None:
    # Prefer the Authorization header, fall back to a `?token=` query param.
    # The query-param form is used by <img>/<link> requests, which cannot set
    # an Authorization header (e.g. the dashboard profile photo).
    header = request.headers.get("Authorization", "")
    token = ""
    if header.startswith("Bearer "):
        token = header[7:].strip()
    elif request.args.get("token"):
        token = request.args.get("token", "").strip()
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=_TTL_PERSIST)
        return data if isinstance(data, dict) else None
    except (BadSignature, SignatureExpired):
        return None


app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


def _require_user() -> dict:
    data = _read_token()
    if not data:
        raise PermissionError("Not authenticated")
    return data


def _make_admin_token(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id, "admin": True})


def _require_admin() -> dict:
    data = _read_token()
    if not data or not data.get("admin"):
        raise PermissionError("Not authenticated")
    # The signed token's `admin` flag is trusted (issued only after a valid
    # username/password login), so we don't re-check against a Telegram UID.
    return data


# ---------------------------------------------------------------- static

@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/dashboard")
def dashboard():
    return _no_cache(send_from_directory(WEB_DIR, "dashboard.html"))


@app.route("/login")
def login_page():
    return _no_cache(send_from_directory(WEB_DIR, "login.html"))


@app.route("/<path:path>")
def static_files(path):
    if path.endswith((".html", ".js")):
        return _no_cache(send_from_directory(WEB_DIR, path))
    return send_from_directory(WEB_DIR, path)


# ---------------------------------------------------------------- api

@app.route("/api/status")
def api_status():
    return jsonify({"status": "ok", "web_url": WEB_URL})


@app.get("/api/config")
def api_config():
    """Expose public config to the dashboard client (e.g. Firebase web config
    for Google sign-in). Only includes NON-secret values."""
    firebase_web = None
    if FIREBASE_PROJECT_ID:
        firebase_web = {
            "apiKey": FIREBASE_WEB_API_KEY,
            "authDomain": FIREBASE_AUTH_DOMAIN or f"{FIREBASE_PROJECT_ID}.firebaseapp.com",
            "projectId": FIREBASE_PROJECT_ID,
            "storageBucket": FIREBASE_STORAGE_BUCKET or f"{FIREBASE_PROJECT_ID}.appspot.com",
            "messagingSenderId": FIREBASE_MESSAGING_SENDER_ID,
            "appId": FIREBASE_WEB_APP_ID,
        }
    return jsonify({
        "github_enabled": bool(GITHUB_CLIENT_ID),
        "firebase_enabled": bool(FIREBASE_PROJECT_ID),
        "firebase_web": firebase_web if (firebase_web and firebase_web.get("apiKey") and firebase_web.get("appId")) else None,
    })


@app.post("/api/register")
def api_register():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").lower().strip()
    password = body.get("password") or ""
    confirm = body.get("confirm_password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "Enter a valid email address"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if password != confirm:
        return jsonify({"error": "Passwords do not match"}), 400

    if database.db_get_user_by_email(email):
        return jsonify({"error": "An account with this email already exists. Please log in."}), 409

    try:
        user_id = database.db_create_user(email, generate_password_hash(password), display_name=email.split("@")[0])
    except Exception as e:
        logger.error("register failed: %s", e)
        return jsonify({"error": "Could not create account"}), 500

    database.db_add_active_user(user_id)
    _notify_owner_new_user_sync(user_id, email)
    token = _make_token(user_id, remember=_remember_from(body), auth="email")
    return jsonify({"token": token, "user_id": user_id, "email": email}), 201


@app.post("/api/login")
def api_login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").lower().strip()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    user = database.db_get_user_by_email(email)
    if not user or not user.get("password_hash"):
        return jsonify({"error": "Invalid email or password"}), 401
    if not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid email or password"}), 401

    user_id = user["id"]
    database.db_add_active_user(user_id)
    token = _make_token(user_id, remember=_remember_from(body), auth="email")
    return jsonify({"token": token, "user_id": user_id, "email": email})


# --- Firebase (Google) sign-in ---

_firebase_ready = False


def _firebase_auth():
    """Return a firebase_admin auth module if configured, else None."""
    global _firebase_ready
    if _firebase_ready:
        import firebase_admin.auth as fauth
        return fauth
    if not FIREBASE_CREDENTIALS or not FIREBASE_PROJECT_ID:
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials
        if not firebase_admin._apps:
            if os.path.exists(FIREBASE_CREDENTIALS):
                cred = credentials.Certificate(FIREBASE_CREDENTIALS)
            else:
                import json
                cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
            firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DATABASE_URL or None})
        _firebase_ready = True
        return firebase_admin.auth
    except Exception as e:
        logger.error("Firebase init failed: %s", e)
        return None


@app.post("/api/auth/firebase")
def api_firebase_auth():
    body = request.get_json(silent=True) or {}
    id_token = (body.get("id_token") or "").strip()
    if not id_token:
        return jsonify({"error": "Missing Firebase token"}), 400

    fauth = _firebase_auth()
    if fauth is None:
        return jsonify({"error": "Firebase Google sign-in is not configured on the server."}), 500
    try:
        decoded = fauth.verify_id_token(id_token)
    except Exception as e:
        logger.error("Firebase token verify failed: %s", e)
        return jsonify({"error": "Invalid Firebase token"}), 401

    uid = decoded.get("uid")
    email = (decoded.get("email") or "").lower().strip()
    name = decoded.get("name") or ""
    photo = decoded.get("picture") or ""
    if not email:
        return jsonify({"error": "Google account has no email."}), 400

    user_id = database.db_upsert_oauth_user("google", uid, email, name, photo)
    database.db_add_active_user(user_id)
    if photo:
        database.db_update_user_photo(user_id, photo)
    token = _make_token(user_id, remember=_remember_from(body), auth="google")
    return jsonify({"token": token, "user_id": user_id, "email": email})


# --- GitHub sign-in ---

@app.get("/auth/github")
def auth_github_start():
    if not GITHUB_CLIENT_ID:
        return jsonify({"error": "GitHub sign-in is not configured on the server."}), 500
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "scope": "read:user user:email",
        "state": state,
        "allow_signup": "true",
    }
    return redirect("https://github.com/login/oauth/authorize?" + urlencode(params))


@app.get("/auth/github/callback")
def auth_github_callback():
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return redirect("/login?auth=error")
    code = request.args.get("code")
    if not code:
        return redirect("/login?auth=cancel")
    try:
        token_resp = requests.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token")
    except Exception as e:
        logger.error("GitHub token exchange failed: %s", e)
        return redirect("/login?auth=error")
    if not access_token:
        return redirect("/login?auth=error")

    try:
        headers = {"Authorization": f"token {access_token}"}
        user_resp = requests.get("https://api.github.com/user", headers=headers, timeout=20)
        user_resp.raise_for_status()
        gdata = user_resp.json()

        if not gdata.get("email"):
            try:
                emails_resp = requests.get("https://api.github.com/user/emails", headers=headers, timeout=20)
                for e in emails_resp.json():
                    if e.get("primary") and e.get("verified"):
                        gdata["email"] = e["email"]
                        break
            except Exception:
                pass
        email = (gdata.get("email") or "").lower().strip()
        if not email:
            email = f"{gdata.get('login','gh')}@users.noreply.github.com"
        name = gdata.get("name") or gdata.get("login") or ""
        photo = gdata.get("avatar_url") or ""
        user_id = database.db_upsert_oauth_user("github", str(gdata.get("id")), email, name, photo)
        database.db_add_active_user(user_id)
        if photo:
            database.db_update_user_photo(user_id, photo)
        token = _make_token(user_id, auth="github")
        return redirect("/login?auth=success&token=" + token)
    except Exception as e:
        logger.error("GitHub user fetch failed: %s", e)
        return redirect("/login?auth=error")


def _notify_owner_new_user_sync(user_id: int, email: str) -> None:
    try:
        _notify_owner_new_user(user_id, email)
    except Exception:
        pass


def _notify_owner_new_user(user_id: int, email: str) -> None:
    try:
        import bot as hostbot
        text = (
            f"👤 **New Web User!**\n\n"
            f"Email: {email}\n"
            f"User ID: `{user_id}`"
        )
        hostbot._notify_user_sync(OWNER_ID, text)
    except Exception as e:
        logger.error("notify owner new web user: %s", e)


@app.get("/api/me")
def api_me():
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    uid = data["uid"]
    profile = database.db_get_profile(uid) or {}
    user = database.db_get_user_by_id(uid) or {}
    # OAuth photo overrides Telegram profile photo
    has_photo = bool(profile.get("photo_key")) or bool(user.get("photo_url"))
    return jsonify(
        {
            "user_id": uid,
            "first_name": user.get("display_name") or profile.get("first_name"),
            "username": profile.get("username"),
            "email": user.get("email"),
            "bio": profile.get("bio"),
            "photo_url": user.get("photo_url"),
            "auth_method": user.get("auth_method"),
            "has_photo": has_photo,
        }
    )


@app.get("/api/me/photo")
def api_me_photo():
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    profile = database.db_get_profile(data["uid"]) or {}
    user = database.db_get_user_by_id(data["uid"]) or {}
    photo_url = user.get("photo_url")
    if photo_url:
        try:
            r = requests.get(photo_url, timeout=10)
            if r.ok:
                return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))
        except Exception as e:
            logger.error("fetch oauth photo: %s", e)
    photo_key = profile.get("photo_key")
    if not photo_key:
        return jsonify({"error": "No photo"}), 404
    try:
        body = r2_download(photo_key)
    except Exception:
        return jsonify({"error": "Photo unavailable"}), 404
    return Response(body, mimetype="image/jpeg")


def _owned_bot(bot_id: int, user_id: int) -> dict | None:
    b = database.db_get_bot(bot_id, user_id)
    if b is None:
        # allow admins to manage any bot
        b = database.db_get_bot_any(bot_id)
        if b is None or user_id not in database.db_load_admins():
            return None
    return b


def _bot_payload(b: dict) -> dict:
    return {
        "id": b["id"],
        "filename": b.get("filename"),
        "file_type": b.get("file_type"),
        "status": b.get("status"),
        "approval": b.get("approval"),
        "env_file_found": b.get("env_file_found"),
        "env_keys": get_env_keys(b["id"]),
        "created_at": b.get("created_at"),
    }


@app.get("/api/bots")
def api_bots():
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    rows = database.db_get_user_bots(data["uid"])
    return jsonify({"bots": [_bot_payload(r) for r in rows]})


def _submitted_bot_payload(b: dict) -> dict:
    return {
        "id": b["id"],
        "filename": b.get("filename"),
        "file_type": b.get("file_type"),
        "status": b.get("status"),
        "approval": b.get("approval"),
        "env_file_found": b.get("env_file_found"),
        "env_keys": get_env_keys(b["id"]),
    }


def _create_bot_from_bytes(user_id: int, filename: str, data: bytes, file_type: str) -> int:
    """Shared internal: create a bot row, upload bytes to R2, encrypt envs,
    and notify admins. Returns bot_id."""
    from env_manager import detect_dependencies, detect_dependencies_from_zip

    if file_type == "zip":
        detected = detect_dependencies_from_zip(data)
    else:
        detected = detect_dependencies(data)

    env_found = False
    env_vars: dict = {}
    if file_type == "zip":
        from env_manager import inspect_zip_safety, find_env_files, pick_primary_env, parse_env_file
        safe, _ = inspect_zip_safety(data)
        if safe:
            env_files = find_env_files(data)
            if env_files:
                primary = pick_primary_env(env_files)
                if primary:
                    try:
                        with zipfile.ZipFile(io.BytesIO(data)) as zf:
                            content = zf.read(primary).decode("utf-8", errors="replace")
                            parsed = parse_env_file(content)
                            if parsed:
                                env_found = True
                                env_vars = parsed
                    except Exception as e:
                        logger.error("web zip env parse: %s", e)

    bot_id = database.db_add_bot(user_id, filename, file_type, r2_key="", env_found=env_found, packages=detected)
    try:
        r2_key = r2_upload(user_id, bot_id, filename, data)
        database.db_update_bot(bot_id, r2_key=r2_key)
    except Exception as e:
        logger.error("web r2 upload bot=%d: %s", bot_id, e)
        database.db_delete_bot(bot_id)
        raise RuntimeError("Cloud storage upload failed")

    if env_found and env_vars:
        try:
            from env_manager import encrypt_env_vars
            encrypt_env_vars(bot_id, env_vars)
        except Exception as e:
            logger.error("web env encrypt bot=%d: %s", bot_id, e)

    try:
        import bot as hostbot
        hostbot.save_user_file(user_id, filename, file_type)
        for aid in database.db_load_admins():
            try:
                hostbot._notify_user_sync(
                    aid,
                    f"📄 **New File for Approval**\n\n👤 User: `{user_id}`\n📛 File: `{filename}`\n📊 Type: {file_type}\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                )
            except Exception:
                pass
    except Exception as e:
        logger.error("web notify admins: %s", e)

    return bot_id


@app.post("/api/bots/upload")
def api_upload_bot():
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    uid = data["uid"]
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    filename = f.filename or "bot.zip"
    ext = Path(filename).suffix.lower()
    if ext not in {".py", ".zip"}:
        return jsonify({"error": "Only .py or .zip files are allowed"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"error": "Empty file"}), 400

    from config import MAX_UPLOAD_BYTES, MAX_UPLOAD_MB
    if len(raw) > MAX_UPLOAD_BYTES:
        return jsonify({"error": f"File too large. Max {MAX_UPLOAD_MB} MB"}), 400

    file_type = "zip" if ext == ".zip" else "py"
    try:
        bot_id = _create_bot_from_bytes(uid, filename, raw, file_type)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    b = database.db_get_bot_any(bot_id) or {}
    return jsonify({"bot": _submitted_bot_payload(b)}), 201


@app.post("/api/bots/git")
def api_git_clone():
    """Clone a git repo and create a bot from it. Accepts JSON:
    { repo_url, token? } where token is an optional GitHub PAT for private repos.
    Uses the connected account's API token when not provided via client."""
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    uid = data["uid"]
    body = request.get_json(silent=True) or {}
    repo_url = (body.get("repo_url") or "").strip()
    if not repo_url:
        return jsonify({"error": "Repo URL is required"}), 400
    if not (repo_url.startswith("https://github.com/") or repo_url.startswith("git@github.com:")):
        return jsonify({"error": "Only GitHub repository URLs are supported"}), 400

    token_provided = (body.get("token") or "").strip()

    import subprocess
    work = Path(tempfile.mkdtemp(prefix="gitclone_"))
    try:
        if repo_url.startswith("git@github.com:"):
            cmd = ["git", "clone", "--depth", "1", "--filter=blob:none", repo_url, str(work / "repo")]
        elif token_provided:
            # inject token into the URL
            auth_repo = repo_url.replace("https://github.com/", f"https://x-access-token:{token_provided}@github.com/")
            cmd = ["git", "clone", "--depth", "1", "--filter=blob:none", auth_repo, str(work / "repo")]
        else:
            cmd = ["git", "clone", "--depth", "1", "--filter=blob:none", repo_url, str(work / "repo")]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            logger.error("git clone failed: %s", r.stderr[-1000:])
            return jsonify({"error": f"Clone failed. Check the URL{'. Private repos need a token.' if not token_provided else ''}"}), 400

        repo_dir = work / "repo"
        # Find a main script (.py) or a zip-able bundle
        py_files = list(repo_dir.rglob("*.py"))
        entry = None
        if (repo_dir / "bot.py").exists():
            entry = repo_dir / "bot.py"
        elif (repo_dir / "main.py").exists():
            entry = repo_dir / "main.py"
        elif py_files:
            entry = py_files[0]

        if entry is None:
            return jsonify({"error": "No .py entry file found in the repository"}), 400

        from security import encrypt_value
        from env_manager import parse_env_file
        env_vars = {}
        env_file = repo_dir / ".env"
        if env_file.exists():
            env_vars = parse_env_file(env_file.read_text(encoding="utf-8", errors="replace"))

        parent = entry.parent if entry.parent != repo_dir else None
        if parent:
            # zip the whole subfolder containing the entry as the project
            import shutil as _shutil
            arc_path = repo_dir
            zip_buf = io.BytesIO()
            env_found = bool(env_vars)
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for fpath in _shutil_iter_files(arc_path):
                    try:
                        zf.write(fpath, fpath.relative_to(arc_path).as_posix())
                    except Exception:
                        pass
            data = zip_buf.getvalue()
            file_type = "zip"
            filename = repo_url.rstrip("/").split("/")[-1].replace(".git", "") + ".zip"
        else:
            data = entry.read_bytes()
            file_type = "py"
            filename = repo_url.rstrip("/").split("/")[-1].replace(".git", "") + ".py"
            env_found = bool(env_vars)

        bot_id = database.db_add_bot(uid, filename, file_type, r2_key="", env_found=env_found, packages=[])
        try:
            r2_key = r2_upload(uid, bot_id, filename, data)
            database.db_update_bot(bot_id, r2_key=r2_key)
        except Exception as e:
            database.db_delete_bot(bot_id)
            return jsonify({"error": f"Cloud storage upload failed: {e}"}), 500

        if env_vars:
            from env_manager import encrypt_env_vars
            encrypt_env_vars(bot_id, env_vars)

        try:
            import bot as hostbot
            hostbot.save_user_file(uid, filename, file_type)
            for aid in database.db_load_admins():
                try:
                    hostbot._notify_user_sync(
                        aid,
                        f"📄 **New Git Clone for Approval**\n\n👤 User: `{uid}`\n📛 Repo: `{repo_url}`\n📊 File: `{filename}`\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error("web git notify admins: %s", e)

        return jsonify({"bot": _submitted_bot_payload(database.db_get_bot_any(bot_id) or {})}), 201
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Clone timed out (repo too large)"}), 400
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _shutil_iter_files(base: Path):
    for p in base.rglob("*"):
        if p.is_file():
            yield p


@app.get("/api/bots/<int:bot_id>")
def api_bot_detail(bot_id: int):
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    b = _owned_bot(bot_id, data["uid"])
    if not b:
        return jsonify({"error": "Bot not found"}), 404
    return jsonify({"bot": _bot_payload(b)})


@app.post("/api/bots/<int:bot_id>/start")
def api_start(bot_id: int):
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    b = _owned_bot(bot_id, data["uid"])
    if not b:
        return jsonify({"error": "Bot not found"}), 404

    import bot as hostbot
    err = hostbot.start_bot_docker(bot_id)
    if err:
        return jsonify({"error": err}), 400
    fresh = hostbot.db_get_bot_any(bot_id) or b
    return jsonify({"bot": _bot_payload(fresh)})


@app.post("/api/bots/<int:bot_id>/stop")
def api_stop(bot_id: int):
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    b = _owned_bot(bot_id, data["uid"])
    if not b:
        return jsonify({"error": "Bot not found"}), 404

    import bot as hostbot
    hostbot.stop_bot_docker(bot_id)
    fresh = hostbot.db_get_bot_any(bot_id) or b
    return jsonify({"bot": _bot_payload(fresh)})


@app.post("/api/bots/<int:bot_id>/restart")
def api_restart(bot_id: int):
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    b = _owned_bot(bot_id, data["uid"])
    if not b:
        return jsonify({"error": "Bot not found"}), 404

    import bot as hostbot
    hostbot.stop_bot_docker(bot_id)
    err = hostbot.start_bot_docker(bot_id)
    if err:
        return jsonify({"error": err}), 400
    fresh = hostbot.db_get_bot_any(bot_id) or b
    return jsonify({"bot": _bot_payload(fresh)})


@app.get("/api/bots/<int:bot_id>/logs")
def api_logs(bot_id: int):
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    b = _owned_bot(bot_id, data["uid"])
    if not b:
        return jsonify({"error": "Bot not found"}), 404

    cn = b.get("container_name")
    if not cn or not docker_exists(cn):
        return jsonify({"logs": "", "running": False})
    logs = docker_logs(cn, tail=200)
    return jsonify({"logs": logs, "running": True})


@app.get("/api/bots/<int:bot_id>/env")
def api_get_env(bot_id: int):
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    b = _owned_bot(bot_id, data["uid"])
    if not b:
        return jsonify({"error": "Bot not found"}), 404
    env_vars = decrypt_env_vars(bot_id)
    # Return the key and its actual value so the dashboard can show/edit/delete them.
    return jsonify({"env": [{"key": k, "value": env_vars[k]} for k in sorted(env_vars.keys())]})


@app.post("/api/bots/<int:bot_id>/env")
def api_set_env(bot_id: int):
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    b = _owned_bot(bot_id, data["uid"])
    if not b:
        return jsonify({"error": "Bot not found"}), 404

    payload = request.get_json(silent=True) or {}
    key = (payload.get("key") or "").strip()
    value = payload.get("value", "")
    if not key:
        return jsonify({"error": "Key is required"}), 400

    database.db_save_env(bot_id, key, encrypt_value(str(value)))
    env_vars = decrypt_env_vars(bot_id)
    return jsonify({"env": [{"key": k, "value": env_vars[k]} for k in sorted(env_vars.keys())]})


@app.delete("/api/bots/<int:bot_id>/env/<path:key>")
def api_delete_env(bot_id: int, key: str):
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    b = _owned_bot(bot_id, data["uid"])
    if not b:
        return jsonify({"error": "Bot not found"}), 404
    database.db_delete_env(bot_id, key)
    env_vars = decrypt_env_vars(bot_id)
    return jsonify({"env": [{"key": k, "value": env_vars[k]} for k in sorted(env_vars.keys())]})


@app.delete("/api/bots/<int:bot_id>")
def api_delete_bot(bot_id: int):
    try:
        data = _require_user()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    b = _owned_bot(bot_id, data["uid"])
    if not b:
        return jsonify({"error": "Bot not found"}), 404

    if b["status"] == "running":
        import bot as hostbot
        hostbot.stop_bot_docker(bot_id)

    try:
        if b.get("r2_key"):
            r2_delete(b["r2_key"])
    except Exception as e:
        logger.error("R2 delete bot=%d: %s", bot_id, e)

    database.db_delete_all_envs(bot_id)

    d = RUNTIME_DIR / str(bot_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)

    database.db_delete_bot(bot_id)
    return jsonify({"deleted": True})


# ---------------------------------------------------------------- admin

MAINTENANCE_NOTICE = (
    "🔧 <b>The bot is currently under maintenance.</b>\n\n"
    "Please try again later. Thank you for your patience!"
)


@app.post("/api/track/pageview")
def api_track_pageview():
    database.db_track_pageview()
    return jsonify({"ok": True})


@app.get("/api/admin/stats")
def api_admin_stats():
    try:
        _require_admin()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    visits = database.db_visit_stats(14)
    stats = {
        "users": database.db_user_stats(),
        "visits": {"total": visits["total"], "today": visits["today"], "this_week": visits["this_week"]},
        "visit_series": visits["series"],
    }
    return jsonify(stats)


@app.post("/api/admin/login")
def api_admin_login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "")
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if not ADMIN_PASSWORD:
        return jsonify({"error": "Admin password is not configured on the server"}), 500
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid username or password"}), 401
    return jsonify({"token": _make_admin_token(OWNER_ID), "admin": True})


@app.get("/api/admin/me")
def api_admin_me():
    try:
        data = _require_admin()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"admin": True, "user_id": data["uid"]})


@app.get("/api/admin/maintenance")
def api_admin_maintenance_get():
    try:
        _require_admin()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    on = database.db_is_maintenance()
    notice = database.db_get_setting("maintenance_notice", MAINTENANCE_NOTICE)
    return jsonify({"maintenance": on, "notice": notice})


@app.post("/api/admin/maintenance")
def api_admin_maintenance_set():
    try:
        _require_admin()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    body = request.get_json(silent=True) or {}
    on = bool(body.get("maintenance"))
    database.db_set_setting("maintenance", on)
    notice = (body.get("notice") or "").strip() or MAINTENANCE_NOTICE
    database.db_set_setting("maintenance_notice", notice)
    logger.warning("Maintenance set to %s by admin %d", on, _read_token()["uid"])
    if on:
        database.db_save_outbox("broadcast", notice)
    return jsonify({"maintenance": on, "notice": notice})


@app.post("/api/admin/broadcast")
def api_admin_broadcast():
    try:
        _require_admin()
    except PermissionError:
        return jsonify({"error": "Not authenticated"}), 401
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Message is required"}), 400
    database.db_save_outbox("broadcast", text)
    return jsonify({"queued": True})


# ---------------------------------------------------------------- run

def main() -> None:
    database.init_db()
    port = int(os.environ.get("DASHBOARD_PORT", "8000"))
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    logger.info("Dashboard running on http://%s:%s", host, port)
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
