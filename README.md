# HostBot - Telegram Bot Hosting Platform

Upload Python bots via Telegram, store on Cloudflare R2, run in Docker containers on a VPS. Admin approval workflow, encrypted environment variables, subscription tiers.

## Architecture

```
User (Telegram)
      |
      v
  +------------------+
  |     bot.py       |  Pyrogram client, command handlers, callback router
  |   (orchestrator) |
  +--------+---------+
           |
  +--------+---------------------------------------------+
  |        |              |              |                |
  v        v              v              v                v
config   database      r2_storage   env_manager    docker_manager
.py       .py            .py          .py             .py
  |        |              |              |                |
  v        v              v              v                v
.env    MongoDB Atlas   Cloudflare R2   Fernet        Docker API
        (bots, envs,                   encryption   (python:3.12-slim)
         subs, admins)
```

### Module Breakdown

| Module | Responsibility |
|---|---|
| `bot.py` | Main orchestrator. All Telegram commands, inline buttons, callback routing, text state machine, broadcast, AI chat (`/mpx`). |
| `config.py` | Loads `.env`, exports all constants (OWNER_ID, limits, Docker settings), sets up logging. |
| `database.py` | MongoDB CRUD. Collections: `bots`, `bot_env`, `subscriptions`, `active_users`, `admins`. |
| `r2_storage.py` | Cloudflare R2 upload/download/delete. Path: `users/<user_id>/<bot_id>/project.zip`. |
| `env_manager.py` | ZIP safety inspection, `.env` file detection/parsing, Fernet encrypt/decrypt of env vars, writes `.env` to VPS at deploy time. |
| `docker_manager.py` | Docker container lifecycle. Security constraints: non-root, cap-drop ALL, read-only /tmp, memory/CPU/PID limits. Container naming: `hostbot_<user_id>_<bot_id>`. |
| `security.py` | Fernet encryption utilities. `encrypt_value`, `decrypt_value`, `generate_key`. |

### Data Flow

```
1. User sends .py/.zip to bot
2. bot.py downloads file via Pyrogram
3. ZIP inspected for safety (path traversal, zip bombs)
4. .env file detected inside ZIP if present
5. Env vars encrypted with Fernet, stored in MongoDB (bot_env)
6. File uploaded to Cloudflare R2: users/<uid>/<bot_id>/project.zip
7. Bot record created in MongoDB (bots collection)
8. Admins notified for approval

--- On approval ---

9. Admin clicks Approve
10. User notified

--- On /startbot ---

11. File downloaded from R2
12. Extracted to runtime/<bot_id>/
13. Encrypted env vars decrypted, written as .env file
14. Docker container started with security constraints
15. Container name tracked in MongoDB
```

### Security Model

| Layer | Detail |
|---|---|
| **File upload** | Max 100 MB, `.py`/`.zip` only, ZIP path traversal protection, zip bomb detection |
| **Env vars** | Fernet encryption at rest in MongoDB, decrypted only at deploy time on VPS |
| **Docker** | `--cap-drop ALL`, `--read-only /tmp`, `--memory 512m`, `--cpus 1`, `--pids-limit 128`, non-root user |
| **Access** | Owner (full), Admins (approve/reject/manage), Free users (2 bots), Pro users (15 bots) |

### Landing Page

Static site in `web/` — deployed on Vercel.

| File | Role |
|---|---|
| `web/index.html` | 9 sections: hero, trust bar, features, how it works, pricing, testimonials, FAQ, CTA, footer |
| `web/style.css` | Dark theme, glassmorphism cards, custom animations |
| `web/script.js` | IntersectionObserver reveals, mobile nav, terminal typing, FAQ accordion |
| `vercel.json` | Routes `web/` as output directory, security headers, asset caching |

## Project Structure

```
hosting-bot/
├── bot.py              # Main orchestrator (commands, callbacks, states)
├── config.py           # Environment variables, constants, logging
├── database.py         # MongoDB collections and CRUD
├── r2_storage.py       # Cloudflare R2 file operations
├── env_manager.py      # ZIP inspection, .env parsing, encryption
├── docker_manager.py   # Docker container lifecycle
├── security.py         # Fernet encryption utilities
├── web/                # Landing page (Vercel)
│   ├── index.html
│   ├── style.css
│   └── script.js
├── vercel.json         # Vercel deployment config
├── requirements.txt    # Python dependencies
├── .env.example        # Environment template (placeholders)
├── .gitignore
├── setup_vps.sh        # VPS provisioning script
└── hosting-bot.service # systemd unit file
```

## Stack

- **Bot**: Pyrogram + TgCrypto
- **Database**: MongoDB Atlas (pymongo)
- **Storage**: Cloudflare R2 (boto3)
- **Execution**: Docker (python:3.12-slim)
- **Encryption**: Fernet (cryptography)
- **Landing**: Vanilla HTML/CSS/JS + Tailwind CDN + Phosphor Icons
- **Hosting**: VPS (bot) + Vercel (landing page)

## Setup

```bash
# 1. Clone
git clone https://github.com/abhisheek2006/host.git
cd host

# 2. VPS setup (installs Docker, Python 3.14, generates encryption key)
bash setup_vps.sh

# 3. Create .env
cp .env.example .env
nano .env  # fill in tokens, keys, encryption key from setup output

# 4. Install & run
python3.14 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Commands

| Command | Who | Description |
|---|---|---|
| `/start` | Everyone | Main menu with inline buttons |
| `/bots` | Everyone | List your bots with status |
| `/env <bot_id>` | Owner/Admin | View encrypted env variables |
| `/help` | Everyone | Help text |
| `/cancel` | Everyone | Cancel current action |
| `/ping` | Everyone | Latency check |
| `/mpx <query>` | Everyone | AI chat (A4F API) |
| `/pending` | Admin | List bots awaiting approval |
