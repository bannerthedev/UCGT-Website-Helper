import os
import threading
import asyncio
from urllib.parse import urlencode

import requests
from flask import Flask, redirect, request, session, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

import discord
from discord.ext import commands

load_dotenv()

app = Flask(__name__)

# -----------------------------
# Environment Variables
# -----------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
SCORES_CHANNEL_ID = int(os.getenv("SCORES_CHANNEL_ID", "0"))

SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-secret")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://ucgtreffingwebsite.netlify.app").strip()
BACKEND_URL = os.getenv("BACKEND_URL", "https://ucgt-website-helper-production.up.railway.app").strip()

REFEREE_ROLE_NAME = os.getenv("REFEREE_ROLE_NAME", "League Referee").strip()
TEAMS_CHANNEL_NAME = os.getenv("TEAMS_CHANNEL_NAME", "teams").strip()

# Fix common BACKEND_URL mistakes
BACKEND_URL = BACKEND_URL.strip()

if BACKEND_URL.endswith("/"):
    BACKEND_URL = BACKEND_URL[:-1]

if not BACKEND_URL.startswith("http://") and not BACKEND_URL.startswith("https://"):
    BACKEND_URL = "https://" + BACKEND_URL

REDIRECT_URI = f"{BACKEND_URL}/auth/callback"

# -----------------------------
# Flask Session / CORS
# -----------------------------

app.secret_key = SESSION_SECRET

app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True
)

CORS(
    app,
    supports_credentials=True,
    origins=[FRONTEND_URL]
)

# -----------------------------
# Discord Bot Setup
# -----------------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

bot_ready = False


@bot.event
async def on_ready():
    global bot_ready
    bot_ready = True
    print(f"Logged in as {bot.user}")


# -----------------------------
# Helpers
# -----------------------------

def is_logged_in():
    return "discord_user" in session and session.get("is_referee") is True


async def get_guild():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        try:
            guild = await bot.fetch_guild(GUILD_ID)
        except Exception:
            return None
    return guild


async def get_member(user_id):
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return None

    member = guild.get_member(int(user_id))

    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except Exception:
            member = None

    return member


async def user_has_referee_role(user_id):
    member = await get_member(user_id)

    if member is None:
        return False

    for role in member.roles:
        if role.name == REFEREE_ROLE_NAME:
            return True

    return False


async def get_teams_from_channel():
    await bot.wait_until_ready()

    teams_channel_id = int(os.getenv("TEAMS_CHANNEL_ID", "0"))
    teams_channel = bot.get_channel(teams_channel_id)

    if teams_channel is None:
        print("TEAMS ERROR: Channel not found by ID.")
        print("TEAMS_CHANNEL_ID:", teams_channel_id)
        return []

    print(f"TEAMS DEBUG: Found channel: #{teams_channel.name}")

    teams = []

    try:
        async for message in teams_channel.history(limit=200, oldest_first=True):
            if message.author.bot:
                continue

            print("TEAMS DEBUG: Raw message content:", repr(message.content))

            # First handle Discord role mentions
            for role in message.role_mentions:
                print("TEAMS DEBUG: Found role mention:", role.name)
                teams.append(role.name)

            # Then handle plain text lines
            lines = message.content.splitlines()

            for line in lines:
                team = line.strip()

                if not team:
                    continue

                # Remove bullets
                while team.startswith("-") or team.startswith("•") or team.startswith("*"):
                    team = team[1:].strip()

                # Skip raw Discord role mentions because we already handled them above
                if team.startswith("<@&") and team.endswith(">"):
                    continue

                # Skip user/channel mentions
                if team.startswith("<@") and team.endswith(">"):
                    continue

                if team.startswith("<#") and team.endswith(">"):
                    continue

                # Skip lines that are only mentions mixed together
                if "<@&" in team:
                    continue

                teams.append(team)

    except discord.Forbidden:
        print("TEAMS ERROR: Bot does not have permission to read the teams channel.")
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


async def send_score_to_discord(team1, team2, winner, score, loser, forfeit=False):
    channel = bot.get_channel(SCORES_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(SCORES_CHANNEL_ID)
        except Exception as e:
            print("Could not find scores channel:", e)
            return False

    message = (
        f"{team1} vs {team2}\n"
        f"> Winner: {winner}\n"
        f"> Score: {score}\n"
        f"> Loser: {loser}"
    )

    if forfeit:
        message += "\n> Team get 5 warnings and then got auto forfeited"

    await channel.send(message)
    return True


def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=15)


# -----------------------------
# Routes
# -----------------------------

@app.route("/")
def home():
    return jsonify({
        "status": "Backend is running",
        "backend_url": BACKEND_URL,
        "redirect_uri": REDIRECT_URI
    })


@app.route("/auth/discord")
def auth_discord():
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify"
    }

    discord_auth_url = "https://discord.com/api/oauth2/authorize?" + urlencode(params)

    print("Discord Auth URL:", discord_auth_url)
    print("Redirect URI:", REDIRECT_URI)

    return redirect(discord_auth_url)


@app.route("/auth/callback")
def auth_callback():
    code = request.args.get("code")

    if not code:
        return redirect(f"{FRONTEND_URL}/denied.html")

    token_url = "https://discord.com/api/oauth2/token"

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    token_response = requests.post(token_url, data=data, headers=headers)

    if token_response.status_code != 200:
        print("Token error:", token_response.text)
        return redirect(f"{FRONTEND_URL}/denied.html")

    token_json = token_response.json()
    access_token = token_json.get("access_token")

    user_response = requests.get(
        "https://discord.com/api/users/@me",
        headers={
            "Authorization": f"Bearer {access_token}"
        }
    )

    if user_response.status_code != 200:
        print("User fetch error:", user_response.text)
        return redirect(f"{FRONTEND_URL}/denied.html")

    user = user_response.json()
    user_id = user["id"]

    try:
        has_role = run_async(user_has_referee_role(user_id))
    except Exception as e:
        print("Role check error:", e)
        has_role = False

    if not has_role:
        session.clear()
        return redirect(f"{FRONTEND_URL}/denied.html")

    session["discord_user"] = {
        "id": user["id"],
        "username": user.get("username"),
        "avatar": user.get("avatar")
    }

    session["is_referee"] = True

    return redirect(f"{FRONTEND_URL}/index.html")


@app.route("/auth/logout")
def logout():
    session.clear()
    return redirect(f"{FRONTEND_URL}/login.html")


@app.route("/api/me")
def api_me():
    if not is_logged_in():
        return jsonify({
            "authenticated": False
        }), 401

    return jsonify({
        "authenticated": True,
        "user": session.get("discord_user")
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
            "error": "Unauthorized"
        }), 401

    data = request.get_json() or {}

    team1 = data.get("team1")
    team2 = data.get("team2")
    winner = data.get("winner")
    score = data.get("score")
    loser = data.get("loser")
    forfeit = bool(data.get("forfeit", False))

    if not team1 or not team2 or not winner or not score or not loser:
        return jsonify({
            "error": "Missing required fields"
        }), 400

    try:
        success = run_async(
            send_score_to_discord(
                team1=team1,
                team2=team2,
                winner=winner,
                score=score,
                loser=loser,
                forfeit=forfeit
            )
        )
    except Exception as e:
        print("Send score error:", e)
        success = False

    if not success:
        return jsonify({
            "error": "Could not send score"
        }), 500

    return jsonify({
        "success": True
    })


# -----------------------------
# Start Bot + Flask
# -----------------------------

def start_bot():
    if not DISCORD_TOKEN:
        print("Missing DISCORD_TOKEN")
        return

    bot.run(DISCORD_TOKEN)


threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
