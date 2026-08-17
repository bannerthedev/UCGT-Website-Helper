import os
import asyncio
import threading

import discord
import requests

from flask import Flask, jsonify, redirect, request, session
from flask_cors import CORS

# =========================
# CONFIG
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "PUT_TOKEN_HERE")
CLIENT_ID = os.getenv("CLIENT_ID", "PUT_CLIENT_ID_HERE")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "PUT_CLIENT_SECRET_HERE")

GUILD_ID = int(os.getenv("GUILD_ID", "123456789012345678"))
SCORES_CHANNEL_ID = int(os.getenv("SCORES_CHANNEL_ID", "123456789012345678"))

TEAM_ROLE_PREFIX = os.getenv("TEAM_ROLE_PREFIX", "Team")
REFEREE_ROLE_NAME = os.getenv("REFEREE_ROLE_NAME", "League Referee")

SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-secret")

BACKEND_URL = os.getenv("BACKEND_URL", "https://your-railway-app.up.railway.app")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://your-netlify-site.netlify.app")

PORT = int(os.getenv("PORT", 5000))

# =========================
# FLASK SETUP
# =========================

app = Flask(__name__)
app.secret_key = SESSION_SECRET

# Needed because Netlify and Railway are different domains.
CORS(
    app,
    supports_credentials=True,
    origins=[
        FRONTEND_URL
    ]
)

# Cookie settings for Netlify <-> Railway session
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True
)

# =========================
# DISCORD BOT SETUP
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = discord.Client(intents=intents)
bot_loop = None


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


def run_bot():
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    bot_loop.run_until_complete(bot.start(DISCORD_TOKEN))


def run_coroutine(coro):
    future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
    return future.result(timeout=20)


async def get_member(user_id):
    guild = bot.get_guild(GUILD_ID)

    if guild is None:
        return None

    try:
        member = guild.get_member(int(user_id))

        if member is None:
            member = await guild.fetch_member(int(user_id))

        return member
    except Exception as e:
        print("get_member error:", e)
        return None


async def get_team_roles():
    guild = bot.get_guild(GUILD_ID)

    if guild is None:
        return []

    teams = []

    for role in guild.roles:
        if role.name.startswith(TEAM_ROLE_PREFIX):
            teams.append({
                "id": str(role.id),
                "name": role.name
            })

    teams.sort(key=lambda x: x["name"].lower())
    return teams


async def discord_send_score(message):
    channel = bot.get_channel(SCORES_CHANNEL_ID)

    if channel is None:
        channel = await bot.fetch_channel(SCORES_CHANNEL_ID)

    await channel.send(message)


def require_referee():
    return session.get("user") and session.get("is_referee")


# =========================
# BASIC ROUTES
# =========================

@app.route("/")
def health():
    return jsonify({
        "status": "online",
        "name": "UCGT Reffing System Backend"
    })


@app.route("/api/status")
def api_status():
    return jsonify({
        "online": True,
        "bot": str(bot.user) if bot.user else None
    })


# =========================
# DISCORD LOGIN
# =========================

@app.route("/auth/discord")
def auth_discord():
    redirect_uri = f"{BACKEND_URL}/auth/discord/callback"

    discord_auth_url = (
        "https://discord.com/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={requests.utils.quote(redirect_uri)}"
        "&response_type=code"
        f"&scope={requests.utils.quote('identify')}"
    )

    return redirect(discord_auth_url)


@app.route("/auth/discord/callback")
def auth_discord_callback():
    code = request.args.get("code")

    if not code:
        return redirect(f"{FRONTEND_URL}/login.html")

    redirect_uri = f"{BACKEND_URL}/auth/discord/callback"

    token_response = requests.post(
        "https://discord.com/api/oauth2/token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri
        }
    )

    token_data = token_response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        print("OAuth token error:", token_data)
        return redirect(f"{FRONTEND_URL}/login.html")

    user_response = requests.get(
        "https://discord.com/api/users/@me",
        headers={
            "Authorization": f"Bearer {access_token}"
        }
    )

    discord_user = user_response.json()
    user_id = discord_user.get("id")

    if not user_id:
        return redirect(f"{FRONTEND_URL}/login.html")

    member = run_coroutine(get_member(user_id))

    if member is None:
        return redirect(f"{FRONTEND_URL}/denied.html")

    has_referee_role = any(role.name == REFEREE_ROLE_NAME for role in member.roles)

    if not has_referee_role:
        return redirect(f"{FRONTEND_URL}/denied.html")

    session["user"] = {
        "id": discord_user.get("id"),
        "username": discord_user.get("username"),
        "avatar": discord_user.get("avatar")
    }

    session["is_referee"] = True

    return redirect(f"{FRONTEND_URL}/index.html")


@app.route("/auth/logout")
def logout():
    session.clear()
    return redirect(f"{FRONTEND_URL}/login.html")


# =========================
# API ROUTES
# =========================

@app.route("/api/me")
def api_me():
    if not require_referee():
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify(session.get("user"))


@app.route("/api/teams")
def api_teams():
    if not require_referee():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        teams = run_coroutine(get_team_roles())
        return jsonify(teams)
    except Exception as e:
        print("teams error:", e)
        return jsonify({"error": "Failed to fetch teams"}), 500


@app.route("/api/send-score", methods=["POST"])
def api_send_score():
    if not require_referee():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}

    team1 = data.get("team1", "Team 1")
    team2 = data.get("team2", "Team 2")
    winner = data.get("winner", "")
    loser = data.get("loser", "")
    score = data.get("score", "")
    auto_forfeit = data.get("autoForfeit", False)

    message = f"""{team1} vs {team2}
> Winner: {winner}
> Score: {score}
> Loser: {loser}"""

    if auto_forfeit:
        message += """
> Team get 5 warnings and then got auto forfeited"""

    try:
        run_coroutine(discord_send_score(message))
        return jsonify({"success": True})
    except Exception as e:
        print("send score error:", e)
        return jsonify({"error": "Failed to send score"}), 500


# =========================
# START APP
# =========================

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    app.run(host="0.0.0.0", port=PORT)
