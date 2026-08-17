import os
import asyncio
import threading
from urllib.parse import urlencode

import discord
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, session
from flask_cors import CORS


# =========================
# Load environment variables
# =========================

load_dotenv()


# =========================
# Flask setup
# =========================

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-key")

FRONTEND_URL = os.getenv("FRONTEND_URL", "").rstrip("/")
BACKEND_URL = os.getenv("BACKEND_URL", "").rstrip("/")

CORS(
    app,
    supports_credentials=True,
    origins=[
        FRONTEND_URL,
        "http://localhost:8888",
        "http://localhost:3000",
        "http://127.0.0.1:8888",
        "http://127.0.0.1:3000",
    ],
)


# =========================
# Discord OAuth variables
# =========================

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

REDIRECT_URI = f"{BACKEND_URL}/auth/callback"

DISCORD_API_BASE = "https://discord.com/api"
DISCORD_AUTH_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"


# =========================
# Discord server/bot variables
# =========================

GUILD_ID = os.getenv("GUILD_ID", "")
TEAMS_CHANNEL_ID = os.getenv("TEAMS_CHANNEL_ID", "")
SCORE_CHANNEL_ID = os.getenv("SCORE_CHANNEL_ID", "")

REFEREE_ROLE_NAME = os.getenv("REFEREE_ROLE_NAME", "Referee")


# =========================
# Discord bot setup
# =========================

bot_loop = None
bot_ready_event = threading.Event()

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True


class UCGTBot(discord.Client):
    async def setup_hook(self):
        global bot_loop
        bot_loop = asyncio.get_running_loop()
        print("Discord bot loop stored successfully.", flush=True)

    async def on_ready(self):
        print(f"Discord bot logged in as {self.user}", flush=True)
        print(
            f"Connected guilds: {[f'{guild.name} ({guild.id})' for guild in self.guilds]}",
            flush=True,
        )
        bot_ready_event.set()


bot = UCGTBot(intents=intents)


def start_bot():
    if not DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN is missing. Bot will not start.", flush=True)
        return

    try:
        print("Starting Discord bot thread...", flush=True)
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as e:
        print(f"Discord bot failed to start: {e}", flush=True)


def run_bot_coroutine(coro, timeout=30):
    if not bot_ready_event.wait(timeout=timeout):
        raise TimeoutError("Discord bot is not ready yet.")

    if bot_loop is None:
        raise RuntimeError("Discord bot loop is not available.")

    future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
    return future.result(timeout=timeout)


def get_guild():
    if not GUILD_ID:
        return bot.guilds[0] if bot.guilds else None

    try:
        guild_id_int = int(GUILD_ID)
    except ValueError:
        return None

    return bot.get_guild(guild_id_int)


async def get_teams_from_channel():
    if not TEAMS_CHANNEL_ID:
        raise ValueError("TEAMS_CHANNEL_ID is missing from Railway variables.")

    try:
        channel_id = int(TEAMS_CHANNEL_ID)
    except ValueError:
        raise ValueError("TEAMS_CHANNEL_ID must be a numeric Discord channel ID.")

    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            raise RuntimeError(f"Could not find teams channel with ID {TEAMS_CHANNEL_ID}: {e}")

    teams = []

    try:
        async for message in channel.history(limit=100):
            # Best option: if the message contains role mentions like @TeamName
            if message.role_mentions:
                for role in message.role_mentions:
                    if role.name not in teams:
                        teams.append(role.name)

            # Fallback: if the channel has plain text team names
            else:
                content = message.content.strip()

                if not content:
                    continue

                lines = content.splitlines()

                for line in lines:
                    cleaned = line.strip()

                    if not cleaned:
                        continue

                    # Remove common bullet/list characters
                    cleaned = cleaned.lstrip("-•*0123456789. ").strip()

                    # Skip raw Discord role mention text like <@&123456789>
                    if cleaned.startswith("<@&") and cleaned.endswith(">"):
                        continue

                    if cleaned and cleaned not in teams:
                        teams.append(cleaned)

    except discord.Forbidden:
        raise PermissionError(
            "Bot does not have permission to read the teams channel. "
            "Give it View Channel and Read Message History permissions."
        )
    except Exception as e:
        raise RuntimeError(f"Failed reading teams channel: {e}")

    teams.sort(key=lambda name: name.lower())
    return teams


async def send_score_to_channel(data):
    if not SCORE_CHANNEL_ID:
        raise ValueError("SCORE_CHANNEL_ID is missing from Railway variables.")

    try:
        channel_id = int(SCORE_CHANNEL_ID)
    except ValueError:
        raise ValueError("SCORE_CHANNEL_ID must be a numeric Discord channel ID.")

    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            raise RuntimeError(f"Could not find score channel with ID {SCORE_CHANNEL_ID}: {e}")

    team1 = data.get("team1", "Team 1")
    team2 = data.get("team2", "Team 2")
    score1 = data.get("score1", 0)
    score2 = data.get("score2", 0)
    reason = data.get("reason", "Final Score")

    embed = discord.Embed(
        title="UCGT Match Result",
        color=discord.Color.blue(),
    )

    embed.add_field(name="Team 1", value=str(team1), inline=True)
    embed.add_field(name="Score", value=str(score1), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(name="Team 2", value=str(team2), inline=True)
    embed.add_field(name="Score", value=str(score2), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(name="Reason", value=str(reason), inline=False)

    await channel.send(embed=embed)

    return True


# =========================
# Auth helpers
# =========================

def get_discord_user(access_token):
    response = requests.get(
        f"{DISCORD_API_BASE}/users/@me",
        headers={
            "Authorization": f"Bearer {access_token}"
        },
        timeout=15,
    )

    if response.status_code != 200:
        return None

    return response.json()


def get_user_guild_member(user_id):
    if not DISCORD_BOT_TOKEN or not GUILD_ID:
        return None

    response = requests.get(
        f"{DISCORD_API_BASE}/guilds/{GUILD_ID}/members/{user_id}",
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
        },
        timeout=15,
    )

    if response.status_code != 200:
        return None

    return response.json()


def user_has_referee_role(member_data):
    if not member_data:
        return False

    guild = get_guild()

    if not guild:
        return False

    role_ids = member_data.get("roles", [])

    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        if role and role.name.lower() == REFEREE_ROLE_NAME.lower():
            return True

    return False


def is_logged_in():
    return bool(session.get("user")) and bool(session.get("is_referee"))


# =========================
# Main/basic routes
# =========================

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "message": "UCGT backend is running."
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok"
    })


# =========================
# Discord OAuth routes
# =========================

@app.route("/auth/discord")
def auth_discord():
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "prompt": "none",
    }

    return redirect(f"{DISCORD_AUTH_URL}?{urlencode(params)}")


@app.route("/auth/callback")
def auth_callback():
    code = request.args.get("code")

    if not code:
        return redirect(f"{FRONTEND_URL}/denied.html")

    token_data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }

    token_headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    token_response = requests.post(
        DISCORD_TOKEN_URL,
        data=token_data,
        headers=token_headers,
        timeout=15,
    )

    if token_response.status_code != 200:
        print(f"Token exchange failed: {token_response.text}", flush=True)
        return redirect(f"{FRONTEND_URL}/denied.html")

    access_token = token_response.json().get("access_token")

    if not access_token:
        return redirect(f"{FRONTEND_URL}/denied.html")

    user = get_discord_user(access_token)

    if not user:
        return redirect(f"{FRONTEND_URL}/denied.html")

    member_data = get_user_guild_member(user["id"])
    has_role = user_has_referee_role(member_data)

    if not has_role:
        session.clear()
        return redirect(f"{FRONTEND_URL}/denied.html")

    session["user"] = {
        "id": user.get("id"),
        "username": user.get("username"),
        "global_name": user.get("global_name"),
        "avatar": user.get("avatar"),
    }
    session["is_referee"] = True

    return redirect(f"{FRONTEND_URL}/index.html")


@app.route("/auth/logout")
def auth_logout():
    session.clear()
    return redirect(f"{FRONTEND_URL}/login.html")


# =========================
# API routes
# =========================

@app.route("/api/me")
def api_me():
    user = session.get("user")

    if not user or not session.get("is_referee"):
        return jsonify({
            "authenticated": False
        }), 401

    return jsonify({
        "authenticated": True,
        "user": user
    })


@app.route("/api/teams")
def api_teams():
    try:
        teams = run_bot_coroutine(get_teams_from_channel())

        return jsonify({
            "teams": teams,
            "count": len(teams)
        })

    except Exception as e:
        print(f"/api/teams error: {e}", flush=True)

        return jsonify({
            "teams": [],
            "count": 0,
            "error": str(e)
        }), 500


@app.route("/api/send-score", methods=["POST"])
def api_send_score():
    if not is_logged_in():
        return jsonify({
            "success": False,
            "error": "Unauthorized"
        }), 401

    data = request.get_json(silent=True) or {}

    required_fields = ["team1", "team2", "score1", "score2"]

    missing = [
        field for field in required_fields
        if field not in data
    ]

    if missing:
        return jsonify({
            "success": False,
            "error": f"Missing fields: {', '.join(missing)}"
        }), 400

    try:
        run_bot_coroutine(send_score_to_channel(data))

        return jsonify({
            "success": True,
            "message": "Score sent successfully."
        })

    except Exception as e:
        print(f"/api/send-score error: {e}", flush=True)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/debug-discord")
def api_debug_discord():
    try:
        guild = get_guild()

        teams_channel = None
        score_channel = None

        if TEAMS_CHANNEL_ID:
            try:
                teams_channel = bot.get_channel(int(TEAMS_CHANNEL_ID))
            except Exception:
                teams_channel = None

        if SCORE_CHANNEL_ID:
            try:
                score_channel = bot.get_channel(int(SCORE_CHANNEL_ID))
            except Exception:
                score_channel = None

        return jsonify({
            "bot_loop_ready": bot_loop is not None,
            "bot_ready": bot_ready_event.is_set(),
            "bot_user": str(bot.user) if bot.user else None,
            "guild_count": len(bot.guilds),
            "guilds": [
                {
                    "id": str(g.id),
                    "name": g.name
                }
                for g in bot.guilds
            ],
            "selected_guild": {
                "id": str(guild.id),
                "name": guild.name
            } if guild else None,
            "teams_channel_id_env": TEAMS_CHANNEL_ID,
            "teams_channel_found": teams_channel is not None,
            "teams_channel_name": teams_channel.name if teams_channel else None,
            "score_channel_id_env": SCORE_CHANNEL_ID,
            "score_channel_found": score_channel is not None,
            "score_channel_name": score_channel.name if score_channel else None,
            "has_discord_bot_token": bool(DISCORD_BOT_TOKEN),
            "has_discord_client_id": bool(DISCORD_CLIENT_ID),
            "has_discord_client_secret": bool(DISCORD_CLIENT_SECRET),
            "guild_id_env": GUILD_ID,
            "referee_role_name": REFEREE_ROLE_NAME,
            "frontend_url": FRONTEND_URL,
            "backend_url": BACKEND_URL,
            "redirect_uri": REDIRECT_URI
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500


# =========================
# Start Discord bot thread
# =========================

bot_thread = threading.Thread(target=start_bot, daemon=True)
bot_thread.start()


# =========================
# Local development only
# =========================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
