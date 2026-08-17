import os
import asyncio
import threading
import requests
import discord

from flask import Flask, redirect, request, jsonify, session
from flask_cors import CORS
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# -----------------------------
# Flask App Setup
# -----------------------------

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key")

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://ucgtreffingwebsite.netlify.app").rstrip("/")
BACKEND_URL = os.getenv("BACKEND_URL", "https://ucgt-website-helper-production.up.railway.app").rstrip("/")

CORS(
    app,
    supports_credentials=True,
    origins=[FRONTEND_URL]
)

# -----------------------------
# Environment Variables
# -----------------------------

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

GUILD_ID = os.getenv("GUILD_ID")
REFEREE_ROLE_NAME = os.getenv("REFEREE_ROLE_NAME", "League Referee")

TEAMS_CHANNEL_ID = os.getenv("TEAMS_CHANNEL_ID")
SCORE_CHANNEL_ID = os.getenv("SCORE_CHANNEL_ID")

REDIRECT_URI = f"{BACKEND_URL}/auth/callback"

DISCORD_API_BASE = "https://discord.com/api"


# -----------------------------
# Discord Bot Setup
# -----------------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot_loop = None
bot_ready_event = threading.Event()


class UCGTBot(commands.Bot):
    async def setup_hook(self):
        global bot_loop
        bot_loop = asyncio.get_running_loop()
        print("Discord bot loop stored successfully.", flush=True)


bot = UCGTBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Discord bot logged in as {bot.user}", flush=True)
    print(f"Connected guilds: {[guild.name for guild in bot.guilds]}", flush=True)
    bot_ready_event.set()


@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Discord bot error in event: {event}", flush=True)


def run_bot_coroutine(coro):
    global bot_loop

    print("Waiting for Discord bot to be ready...", flush=True)

    if not bot_ready_event.wait(timeout=30):
        raise RuntimeError(
            "Discord bot is not ready yet. Check Railway logs to see if the bot token is missing, invalid, or the bot crashed."
        )

    if bot_loop is None:
        raise RuntimeError(
            "Discord bot loop is still not ready. The bot probably failed during startup."
        )

    future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
    return future.result(timeout=30)


def start_bot():
    print("Starting Discord bot thread...", flush=True)

    if not DISCORD_BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN is missing.", flush=True)
        return

    try:
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as e:
        print(f"Discord bot failed to start: {e}", flush=True)


bot_thread = threading.Thread(target=start_bot, daemon=True)
bot_thread.start()


# -----------------------------
# Helper Functions
# -----------------------------

def is_logged_in():
    return "discord_user" in session


def get_current_user():
    return session.get("discord_user")


def get_guild():
    if GUILD_ID:
        guild = bot.get_guild(int(GUILD_ID))
        if guild:
            return guild

    if len(bot.guilds) > 0:
        return bot.guilds[0]

    return None


async def user_has_referee_role(user_id):
    guild = get_guild()

    if not guild:
        print("No guild found.")
        return False

    try:
        member = guild.get_member(int(user_id))

        if member is None:
            member = await guild.fetch_member(int(user_id))

        if member is None:
            print("Member not found in guild.")
            return False

        for role in member.roles:
            if role.name == REFEREE_ROLE_NAME:
                return True

        print(f"User does not have role: {REFEREE_ROLE_NAME}")
        return False

    except Exception as e:
        print(f"Role check error: {e}")
        return False


async def get_teams_from_channel():
    teams = []

    if not TEAMS_CHANNEL_ID:
        print("TEAMS_CHANNEL_ID is missing.")
        return teams

    try:
        channel_id = int(TEAMS_CHANNEL_ID)
    except ValueError:
        print("TEAMS_CHANNEL_ID must be a number.")
        return teams

    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            print(f"Could not fetch teams channel: {e}")
            return teams

    if channel is None:
        print("Teams channel not found.")
        return teams

    try:
        async for message in channel.history(limit=100, oldest_first=True):
            # If message contains role mentions like @Team Name
            if message.role_mentions:
                for role in message.role_mentions:
                    role_name = role.name.strip()

                    if role_name and role_name not in teams:
                        teams.append(role_name)

            # Also parse normal plain text lines
            lines = message.content.splitlines()

            for line in lines:
                cleaned = line.strip()

                if not cleaned:
                    continue

                # Ignore raw role mention IDs like <@&123456789>
                if cleaned.startswith("<@&") and cleaned.endswith(">"):
                    continue

                # Remove common bullet/list formatting
                cleaned = cleaned.lstrip("-").strip()
                cleaned = cleaned.lstrip("•").strip()
                cleaned = cleaned.lstrip("*").strip()

                # Remove numbered list format like 1. Team Name
                if ". " in cleaned:
                    possible_number, possible_team = cleaned.split(". ", 1)
                    if possible_number.isdigit():
                        cleaned = possible_team.strip()

                if cleaned and cleaned not in teams:
                    teams.append(cleaned)

    except discord.Forbidden:
        print("Bot does not have permission to read the teams channel.")
        return teams

    except Exception as e:
        print(f"Error reading teams channel: {e}")
        return teams

    print(f"Loaded teams: {teams}")
    return teams


async def send_score_to_discord(data):
    if not SCORE_CHANNEL_ID:
        raise RuntimeError("SCORE_CHANNEL_ID is missing.")

    try:
        channel_id = int(SCORE_CHANNEL_ID)
    except ValueError:
        raise RuntimeError("SCORE_CHANNEL_ID must be a number.")

    channel = bot.get_channel(channel_id)

    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    if channel is None:
        raise RuntimeError("Score channel not found.")

    team1 = data.get("team1", "Team 1")
    team2 = data.get("team2", "Team 2")
    score1 = data.get("score1", 0)
    score2 = data.get("score2", 0)
    reason = data.get("reason", "Final Score")

    message = (
        "**UCGT Match Result**\n"
        f"**{team1}** {score1} - {score2} **{team2}**\n"
        f"**Reason:** {reason}"
    )

    await channel.send(message)

    return True


# -----------------------------
# Auth Routes
# -----------------------------

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
        "redirect_uri": REDIRECT_URI
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    token_response = requests.post(
        f"{DISCORD_API_BASE}/oauth2/token",
        data=token_data,
        headers=headers
    )

    if token_response.status_code != 200:
        print("Token response error:", token_response.text)
        return redirect(f"{FRONTEND_URL}/denied.html")

    token_json = token_response.json()
    access_token = token_json.get("access_token")

    user_response = requests.get(
        f"{DISCORD_API_BASE}/users/@me",
        headers={
            "Authorization": f"Bearer {access_token}"
        }
    )

    if user_response.status_code != 200:
        print("User response error:", user_response.text)
        return redirect(f"{FRONTEND_URL}/denied.html")

    user = user_response.json()

    has_role = run_bot_coroutine(user_has_referee_role(user["id"]))

    if not has_role:
        return redirect(f"{FRONTEND_URL}/denied.html")

    session["discord_user"] = {
        "id": user.get("id"),
        "username": user.get("username"),
        "global_name": user.get("global_name"),
        "avatar": user.get("avatar")
    }

    return redirect(f"{FRONTEND_URL}/index.html")


@app.route("/auth/logout")
def logout():
    session.clear()
    return redirect(f"{FRONTEND_URL}/login.html")


# -----------------------------
# API Routes
# -----------------------------

@app.route("/api/me")
def api_me():
    if not is_logged_in():
        return jsonify({
            "loggedIn": False
        }), 401

    return jsonify({
        "loggedIn": True,
        "user": get_current_user()
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
        print(f"/api/teams error: {e}")

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
            "error": "Not logged in"
        }), 401

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Missing JSON body"
        }), 400

    try:
        run_bot_coroutine(send_score_to_discord(data))

        return jsonify({
            "success": True
        })

    except Exception as e:
        print(f"/api/send-score error: {e}")

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
            "bot_ready": bot_ready_event.is_set(),
            "bot_user": str(bot.user) if bot.user else None,
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
            "referee_role_name": REFEREE_ROLE_NAME,
            "frontend_url": FRONTEND_URL,
            "backend_url": BACKEND_URL,
            "redirect_uri": REDIRECT_URI
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500


@app.route("/")
def home():
    return jsonify({
        "status": "UCGT backend running",
        "frontend": FRONTEND_URL,
        "backend": BACKEND_URL
    })


# -----------------------------
# Run Flask App
# -----------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port
    )
