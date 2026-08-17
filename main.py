import os
import asyncio
import threading
import requests

from flask import Flask, jsonify, request, session, redirect
from flask_cors import CORS

import discord
from discord.ext import commands


# =========================
# Railway Variables
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
SCORES_CHANNEL_ID = int(os.getenv("SCORES_CHANNEL_ID", "0"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-secret")

# Your Netlify frontend URL
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://your-site.netlify.app")

# Your Railway backend URL
BACKEND_URL = os.getenv("BACKEND_URL", "https://your-backend.up.railway.app")

# Discord role allowed to use the ref website
REFEREE_ROLE_NAME = os.getenv("REFEREE_ROLE_NAME", "League Referee")

# Channel where teams are listed
TEAMS_CHANNEL_NAME = os.getenv("TEAMS_CHANNEL_NAME", "teams")


# =========================
# Flask App
# =========================

app = Flask(__name__)
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


# =========================
# Discord Bot
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


# =========================
# Helpers
# =========================

def is_logged_in():
    return "discord_user_id" in session


def login_required(func):
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            return jsonify({"error": "Not logged in"}), 401

        if not session.get("is_referee"):
            return jsonify({"error": "Access denied"}), 403

        return func(*args, **kwargs)

    wrapper.__name__ = func.__name__
    return wrapper


async def get_member_from_guild(discord_user_id):
    guild = bot.get_guild(GUILD_ID)

    if not guild:
        return None

    try:
        member = guild.get_member(int(discord_user_id))

        if member is None:
            member = await guild.fetch_member(int(discord_user_id))

        return member

    except Exception as e:
        print("Failed to get member:", e)
        return None


def user_has_referee_role(member):
    if member is None:
        return False

    return any(role.name == REFEREE_ROLE_NAME for role in member.roles)


async def get_teams_from_discord_channel():
    guild = bot.get_guild(GUILD_ID)

    if not guild:
        return []

    teams_channel = discord.utils.get(
        guild.text_channels,
        name=TEAMS_CHANNEL_NAME
    )

    if not teams_channel:
        print(f"No Discord channel named #{TEAMS_CHANNEL_NAME} found.")
        return []

    teams = []

    try:
        async for message in teams_channel.history(limit=100):
            if message.author.bot:
                continue

            lines = message.content.splitlines()

            for line in lines:
                team_name = line.strip()

                if not team_name:
                    continue

                # Optional cleanup if people use bullets
                team_name = team_name.removeprefix("-").strip()
                team_name = team_name.removeprefix("•").strip()

                if team_name and team_name not in teams:
                    teams.append(team_name)

    except Exception as e:
        print("Failed to read teams channel:", e)

    return sorted(teams)


def run_async(coro, timeout=10):
    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=timeout)


# =========================
# Auth Routes
# =========================

@app.route("/")
def home():
    return jsonify({"status": "UCGT Reffing System backend online"})


@app.route("/auth/discord")
def auth_discord():
    redirect_uri = f"{BACKEND_URL}/auth/callback"

    discord_auth_url = (
        "https://discord.com/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&response_type=code"
        "&scope=identify"
    )

    return redirect(discord_auth_url)


@app.route("/auth/callback")
def auth_callback():
    code = request.args.get("code")

    if not code:
        return redirect(f"{FRONTEND_URL}/denied.html")

    redirect_uri = f"{BACKEND_URL}/auth/callback"

    token_data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri
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
        print("OAuth token error:", token_response.text)
        return redirect(f"{FRONTEND_URL}/denied.html")

    access_token = token_response.json().get("access_token")

    user_response = requests.get(
        "https://discord.com/api/users/@me",
        headers={
            "Authorization": f"Bearer {access_token}"
        }
    )

    if user_response.status_code != 200:
        print("Discord user fetch error:", user_response.text)
        return redirect(f"{FRONTEND_URL}/denied.html")

    user = user_response.json()
    discord_user_id = user["id"]

    member = run_async(get_member_from_guild(discord_user_id))

    if not user_has_referee_role(member):
        session.clear()
        return redirect(f"{FRONTEND_URL}/denied.html")

    session["discord_user_id"] = discord_user_id
    session["username"] = user.get("username")
    session["avatar"] = user.get("avatar")
    session["is_referee"] = True

    return redirect(f"{FRONTEND_URL}/index.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(f"{FRONTEND_URL}/login.html")


# =========================
# API Routes
# =========================

@app.route("/api/me")
@login_required
def api_me():
    return jsonify({
        "loggedIn": True,
        "id": session.get("discord_user_id"),
        "username": session.get("username"),
        "isReferee": session.get("is_referee", False)
    })


@app.route("/api/teams")
@login_required
def api_teams():
    teams = run_async(get_teams_from_discord_channel())

    return jsonify({
        "teams": teams
    })


@app.route("/api/send-score", methods=["POST"])
@login_required
def api_send_score():
    data = request.get_json() or {}

    team1 = data.get("team1")
    team2 = data.get("team2")
    winner = data.get("winner")
    loser = data.get("loser")
    score = data.get("score")
    auto_forfeit = data.get("autoForfeit", False)
    forfeited_team = data.get("forfeitedTeam")

    if not team1 or not team2 or not winner or not loser or not score:
        return jsonify({"error": "Missing score data"}), 400

    async def send_score_message():
        channel = bot.get_channel(SCORES_CHANNEL_ID)

        if channel is None:
            return False

        message = (
            f"{team1} vs {team2}\n"
            f"> Winner: {winner}\n"
            f"> Score: {score}\n"
            f"> Loser: {loser}"
        )

        if auto_forfeit:
            message += (
                f"\n> {forfeited_team} got 5 warnings and then got auto forfeited"
            )

        await channel.send(message)
        return True

    sent = run_async(send_score_message())

    if not sent:
        return jsonify({"error": "Scores channel not found"}), 404

    return jsonify({
        "success": True,
        "message": "Score posted"
    })


# =========================
# Start Bot and Flask
# =========================

def run_bot():
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()

    port = int(os.getenv("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port
    )
