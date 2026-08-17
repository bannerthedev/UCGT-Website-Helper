import os
import asyncio
import threading
import requests
import discord

from discord.ext import commands
from flask import Flask, request, redirect, jsonify, session
from flask_cors import CORS
from dotenv import load_dotenv


load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key")

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://ucgtreffingwebsite.netlify.app").rstrip("/")
BACKEND_URL = os.getenv("BACKEND_URL", "https://ucgt-website-helper-production.up.railway.app").rstrip("/")

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))

REFEREE_ROLE_NAME = os.getenv("REFEREE_ROLE_NAME", "League Referee")
TEAMS_CHANNEL_ID = int(os.getenv("TEAMS_CHANNEL_ID", "0"))

SCORE_CHANNEL_ID = int(os.getenv("SCORE_CHANNEL_ID", "0"))

REDIRECT_URI = f"{BACKEND_URL}/auth/callback"


CORS(
    app,
    supports_credentials=True,
    origins=[
        FRONTEND_URL,
        "https://ucgtreffingwebsite.netlify.app",
    ],
)


# -----------------------------
# Discord Bot Setup
# -----------------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Discord bot logged in as {bot.user}")
    print(f"Connected guilds: {[guild.name for guild in bot.guilds]}")


def run_bot_coroutine(coro):
    """
    Allows Flask routes, which are normal sync functions, to run async Discord bot code.
    This fixes: name 'run_bot_coroutine' is not defined
    """
    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=20)


def start_bot():
    if not DISCORD_BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN is missing.")
        return

    bot.run(DISCORD_BOT_TOKEN)


bot_thread = threading.Thread(target=start_bot, daemon=True)
bot_thread.start()


# -----------------------------
# Helper Functions
# -----------------------------

def is_logged_in():
    return "discord_user" in session


def get_logged_in_user():
    return session.get("discord_user")


async def user_has_referee_role(user_id):
    await bot.wait_until_ready()

    guild = bot.get_guild(DISCORD_GUILD_ID)

    if guild is None:
        print("ROLE ERROR: Guild not found.")
        return False

    member = guild.get_member(int(user_id))

    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except Exception as e:
            print("ROLE ERROR: Could not fetch member:", repr(e))
            return False

    for role in member.roles:
        if role.name == REFEREE_ROLE_NAME:
            return True

    return False


async def get_teams_from_channel():
    await bot.wait_until_ready()

    if not TEAMS_CHANNEL_ID:
        print("TEAMS ERROR: TEAMS_CHANNEL_ID is missing or invalid.")
        return []

    teams_channel = bot.get_channel(TEAMS_CHANNEL_ID)

    if teams_channel is None:
        print("TEAMS ERROR: Channel not found by ID.")
        print("TEAMS_CHANNEL_ID:", TEAMS_CHANNEL_ID)
        print("Visible channels:")

        for guild in bot.guilds:
            for channel in guild.text_channels:
                print(f"- #{channel.name} | {channel.id}")

        return []

    print(f"TEAMS DEBUG: Found channel #{teams_channel.name} with ID {teams_channel.id}")

    teams = []

    try:
        async for message in teams_channel.history(limit=200, oldest_first=True):
            if message.author.bot:
                continue

            print("TEAMS DEBUG: Raw message:", repr(message.content))

            # Handle Discord role mentions like @Counter Clockwise / CC
            for role in message.role_mentions:
                role_name = role.name.strip()

                if role_name:
                    print("TEAMS DEBUG: Found role mention:", role_name)
                    teams.append(role_name)

            # Handle normal text lines
            lines = message.content.splitlines()

            for line in lines:
                team = line.strip()

                if not team:
                    continue

                # Remove common bullet characters
                while team.startswith("-") or team.startswith("•") or team.startswith("*"):
                    team = team[1:].strip()

                if not team:
                    continue

                # Skip raw role/user/channel mentions
                if team.startswith("<@&") and team.endswith(">"):
                    continue

                if team.startswith("<@") and team.endswith(">"):
                    continue

                if team.startswith("<#") and team.endswith(">"):
                    continue

                # If a line contains role mention syntax, skip it because message.role_mentions handles it better
                if "<@&" in team:
                    continue

                teams.append(team)

    except discord.Forbidden:
        print("TEAMS ERROR: Bot does not have permission to read the teams channel.")
        return []

    except discord.HTTPException as e:
        print("TEAMS ERROR: Discord HTTP error:", repr(e))
        return []

    except Exception as e:
        print("TEAMS ERROR:", repr(e))
        return []

    clean_teams = []
    seen = set()

    for team in teams:
        team = team.strip()

        if not team:
            continue

        key = team.lower()

        if key not in seen:
            clean_teams.append(team)
            seen.add(key)

    print("TEAMS DEBUG: Loaded teams:", clean_teams)

    return clean_teams


async def send_score_to_discord(data):
    await bot.wait_until_ready()

    if not SCORE_CHANNEL_ID:
        print("SCORE ERROR: SCORE_CHANNEL_ID is missing or invalid.")
        return False

    channel = bot.get_channel(SCORE_CHANNEL_ID)

    if channel is None:
        print("SCORE ERROR: Score channel not found.")
        return False

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

@app.route("/")
def home():
    return jsonify({
        "status": "UCGT backend running",
        "frontend": FRONTEND_URL,
        "backend": BACKEND_URL
    })


@app.route("/auth/discord")
def auth_discord():
    discord_auth_url = (
        "https://discord.com/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        "&response_type=code"
        f"&redirect_uri={requests.utils.quote(REDIRECT_URI, safe='')}"
        "&scope=identify%20guilds"
    )

    return redirect(discord_auth_url)


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

    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    token_response = requests.post(
        "https://discord.com/api/oauth2/token",
        data=token_data,
        headers=headers
    )

    if token_response.status_code != 200:
        print("OAUTH ERROR:", token_response.text)
        return redirect(f"{FRONTEND_URL}/denied.html")

    access_token = token_response.json().get("access_token")

    user_response = requests.get(
        "https://discord.com/api/users/@me",
        headers={
            "Authorization": f"Bearer {access_token}"
        }
    )

    if user_response.status_code != 200:
        print("USER ERROR:", user_response.text)
        return redirect(f"{FRONTEND_URL}/denied.html")

    user = user_response.json()

    has_role = run_bot_coroutine(user_has_referee_role(user["id"]))

    if not has_role:
        return redirect(f"{FRONTEND_URL}/denied.html")

    session["discord_user"] = {
        "id": user["id"],
        "username": user.get("username"),
        "avatar": user.get("avatar"),
        "global_name": user.get("global_name"),
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
            "logged_in": False
        }), 401

    return jsonify({
        "logged_in": True,
        "user": get_logged_in_user()
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
        print("API TEAMS ERROR:", repr(e))

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

    data = request.get_json(silent=True) or {}

    try:
        success = run_bot_coroutine(send_score_to_discord(data))

        if not success:
            return jsonify({
                "success": False,
                "error": "Could not send score to Discord"
            }), 500

        return jsonify({
            "success": True
        })

    except Exception as e:
        print("SEND SCORE ERROR:", repr(e))

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# -----------------------------
# Debug Routes
# -----------------------------

@app.route("/api/debug-discord")
def debug_discord():
    try:
        guilds = []

        for guild in bot.guilds:
            channels = []

            for channel in guild.text_channels:
                channels.append({
                    "name": channel.name,
                    "id": str(channel.id)
                })

            guilds.append({
                "name": guild.name,
                "id": str(guild.id),
                "channels": channels
            })

        return jsonify({
            "bot_ready": bot.is_ready(),
            "bot_user": str(bot.user) if bot.user else None,
            "guild_id_env": str(DISCORD_GUILD_ID),
            "teams_channel_id_env": str(TEAMS_CHANNEL_ID),
            "score_channel_id_env": str(SCORE_CHANNEL_ID),
            "guilds": guilds
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500


# -----------------------------
# Run App
# -----------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
