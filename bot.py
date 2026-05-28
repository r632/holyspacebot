import discord
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo
import aiohttp
import asyncio
import sqlite3
from datetime import datetime, timezone
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", "6"))

DB_PATH = os.getenv(
    "DB_PATH",
    "/data/launches.db" if os.path.isdir("/data") else "launches.db"
)

# ── BOT SETUP ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ── DATABASE ─────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS announced (
                launch_id TEXT PRIMARY KEY,
                announced_at TEXT
            )
        """)
        con.commit()


def is_announced(launch_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT 1 FROM announced WHERE launch_id = ?",
            (launch_id,)
        ).fetchone() is not None


def mark_announced(launch_id: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO announced (launch_id, announced_at) VALUES (?, datetime('now'))",
            (launch_id,)
        )
        con.commit()


def reset_announced():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM announced")
        con.commit()


def count_announced():
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT COUNT(*) FROM announced").fetchone()[0]


# ── API ─────────────────────────────────────────────

async def fetch_upcoming_launches(limit=5):
    url = "https://ll.thespacedevs.com/2.3.0/launches/upcoming/"
    params = {
        "limit": limit,
        "ordering": "net",
        "format": "json"
    }

    headers = {
        "User-Agent": "DiscordSpaceLaunchBot/1.0"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                print(f"❌ API error: {resp.status}")
                return []

            data = await resp.json()
            return data.get("results", [])


# ── TIME PARSING SAFE ─────────────────────────────────────────────

def parse_utc(net_str: str):
    """Parse une date API en UTC safe (toujours timezone-aware)."""
    if not net_str:
        return None

    dt = datetime.fromisoformat(net_str.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def format_date_paris(dt_utc: datetime) -> str:
    dt_paris = dt_utc.astimezone(ZoneInfo("Europe/Paris"))

    mois_fr = [
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre"
    ]

    return (
        f"{dt_paris.day} {mois_fr[dt_paris.month - 1]} "
        f"{dt_paris.year} à {dt_paris.strftime('%H:%M')} (heure de Paris)"
    )


def format_countdown(dt_utc: datetime) -> str:
    """Retourne un compte à rebours lisible."""
    now = datetime.now(timezone.utc)
    delta = dt_utc - now

    if delta.total_seconds() <= 0:
        return "Maintenant"

    total_seconds = int(delta.total_seconds())

    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []

    if days > 0:
        parts.append(f"{days}j")

    if hours > 0 or days > 0:
        parts.append(f"{hours}h")

    parts.append(f"{minutes}m")

    return "dans " + " ".join(parts)


def format_window_duration(window_start, window_end):
    """Formate la durée de fenêtre de lancement."""
    if not window_start or not window_end:
        return "Inconnue"

    start = parse_utc(window_start)
    end = parse_utc(window_end)

    if not start or not end:
        return "Inconnue"

    delta = end - start

    if delta.total_seconds() <= 0:
        return "Instantanée"

    total_minutes = int(delta.total_seconds() // 60)

    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours > 0:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


# ── EMBEDS ─────────────────────────────────────────────

def format_launch_embed(launch: dict):
    name = launch.get("name", "Lancement inconnu")
    status = (launch.get("status") or {}).get("name", "Inconnu")
    net = launch.get("net")

    dt_utc = parse_utc(net)

    if dt_utc:
        if dt_utc < datetime.now(timezone.utc):
            return None
        date_str = format_date_paris(dt_utc)
        countdown = format_countdown(dt_utc)
    else:
        date_str = "Date inconnue"
        countdown = "Inconnu"

    color_map = {
        "Go for Launch": discord.Color.green(),
        "TBD": discord.Color.orange(),
        "TBC": discord.Color.gold(),
        "Success": discord.Color.blue(),
        "Failure": discord.Color.red(),
        "Hold": discord.Color.red(),
        "In Flight": discord.Color.teal(),
    }

    embed = discord.Embed(
        title=f"🚀 {name}",
        color=color_map.get(status, discord.Color.blurple())
    )

    embed.add_field(name="📅 Date", value=date_str, inline=False)
    embed.add_field(name="⏱️ Compte à rebours", value=countdown, inline=True)
    embed.add_field(name="📊 Statut", value=status, inline=True)

    rocket = ((launch.get("rocket") or {})
              .get("configuration") or {})

    embed.add_field(
        name="🛸 Fusée",
        value=rocket.get("full_name") or rocket.get("name", "Inconnu"),
        inline=True
    )

    lsp = (launch.get("launch_service_provider") or {}).get("name", "Inconnu")
    embed.add_field(name="🏢 Opérateur", value=lsp, inline=True)

    pad = launch.get("pad") or {}
    loc = (pad.get("location") or {}).get("name", "")

    embed.add_field(
        name="📍 Site",
        value=f"{pad.get('name','Inconnu')}\n{loc}" if loc else pad.get("name", "Inconnu"),
        inline=True
    )

    mission = launch.get("mission")

    orbit = "Inconnue"

    if mission:
        orbit_data = mission.get("orbit")

        if isinstance(orbit_data, dict):
            orbit = orbit_data.get("abbrev") or orbit_data.get("name", "Inconnue")
        elif isinstance(orbit_data, str):
            orbit = orbit_data

        if mission.get("type"):
            embed.add_field(name="🎯 Type", value=mission["type"], inline=True)

        embed.add_field(name="🌍 Orbite", value=orbit, inline=True)

        if mission.get("description"):
            desc = mission["description"][:300]
            if len(mission["description"]) > 300:
                desc += "..."
            embed.add_field(name="📝 Mission", value=desc, inline=False)
    else:
        embed.add_field(name="🌍 Orbite", value=orbit, inline=True)

    window_duration = format_window_duration(
        launch.get("window_start"),
        launch.get("window_end")
    )

    embed.add_field(
        name="🪟 Fenêtre de lancement",
        value=window_duration,
        inline=True
    )

    image = launch.get("image")
    if isinstance(image, dict):
        embed.set_thumbnail(url=image.get("image_url") or image.get("thumbnail_url"))

    embed.set_footer(text="Données : TheSpaceDevs / NextSpaceFlight")
    return embed


def format_urgent_alert_embed(launch: dict):
    """Embed rouge pour les lancements dans moins de 2h."""
    name = launch.get("name", "Lancement inconnu")
    net = launch.get("net")

    dt_utc = parse_utc(net)

    if not dt_utc:
        return None

    remaining = dt_utc - datetime.now(timezone.utc)

    if remaining.total_seconds() <= 0 or remaining.total_seconds() > 7200:
        return None

    embed = discord.Embed(
        title="🚨 LANCEMENT IMMINENT 🚨",
        description=f"**{name}** décolle {format_countdown(dt_utc)} !",
        color=discord.Color.red()
    )

    embed.add_field(
        name="📅 Heure",
        value=format_date_paris(dt_utc),
        inline=False
    )

    return embed


# ── LOOP ─────────────────────────────────────────────

@tasks.loop(hours=CHECK_INTERVAL_HOURS)
async def check_launches():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print("❌ Channel introuvable")
        return

    launches = await fetch_upcoming_launches(limit=10)
    new_launches = []
    urgent_alerts = []

    for launch in launches:
        launch_id = launch.get("id")

        if not launch_id or is_announced(launch_id):
            continue

        embed = format_launch_embed(launch)

        if not embed:
            continue

        new_launches.append(embed)

        urgent_embed = format_urgent_alert_embed(launch)
        if urgent_embed:
            urgent_alerts.append(urgent_embed)

        mark_announced(launch_id)

    # nouveaux lancements d'abord
    for embed in new_launches:
        await channel.send(embed=embed)
        await asyncio.sleep(1)

    # alertes ensuite
    for embed in urgent_alerts:
        await channel.send(embed=embed)
        await asyncio.sleep(1)

    total_new = len(new_launches)

    print(
        f"🚀 {total_new} nouveaux lancements."
        if total_new else
        "ℹ️ Aucun nouveau lancement."
    )


# ── EVENTS ─────────────────────────────────────────────

@bot.event
async def on_ready():
    init_db()

    print(f"✅ Connecté: {bot.user}")
    print(f"📡 Channel: {CHANNEL_ID}")
    print(f"🗄️ DB: {DB_PATH} ({count_announced()} déjà envoyés)")

    if not check_launches.is_running():
        check_launches.start()


# ── COMMANDS ─────────────────────────────────────────────

@bot.command()
async def launches(ctx, limit: int = 5):
    limit = max(1, min(limit, 10))
    SPECIAL_USER_ID = 892136136749776916

    if ctx.author.id == SPECIAL_USER_ID:
        await ctx.send(f"🔭 Récupération des {limit} prochains lancements, mon maître et créateur! :3")
    else:
        await ctx.send(f"🔭 Récupération des {limit} prochains lancements ... :3")

    launches = await fetch_upcoming_launches(limit=20)
    now = datetime.now(timezone.utc)

    valid = []

    for launch in launches:
        net = launch.get("net")
        dt = parse_utc(net)

        if not dt or dt < now:
            continue

        embed = format_launch_embed(launch)

        if not embed:
            continue

        valid.append(embed)

        if len(valid) >= limit:
            break

    if not valid:
        await ctx.send("❌ Aucun lancement valide trouvé.")
        return

    for embed in valid:
        await ctx.send(embed=embed)
        await asyncio.sleep(0.5)


@bot.command()
async def next(ctx):
    SPECIAL_USER_ID = 892136136749776916

    if ctx.author.id == SPECIAL_USER_ID:
        await ctx.send("Voici le prochain lancement, mon maître et créateur! :3")
    else:
        await ctx.send("Voici le prochain lancement :3 🚀")

    launches = await fetch_upcoming_launches(10)

    now = datetime.now(timezone.utc)

    for launch in launches:
        net = launch.get("net")
        dt = parse_utc(net)

        if not dt or dt < now:
            continue

        embed = format_launch_embed(launch)

        if embed:
            await ctx.send(embed=embed)

            urgent_embed = format_urgent_alert_embed(launch)
            if urgent_embed:
                await ctx.send(embed=urgent_embed)

            return

    await ctx.send("❌ Aucun prochain lancement trouvé")


@bot.command()
@commands.has_permissions(administrator=True)
async def reset(ctx):
    reset_announced()
    await ctx.send("✅ Reset OK")


@bot.command()
async def spacehelp(ctx):
    embed = discord.Embed(
        title="🚀 Space Bot",
        description="Bot de suivi des lancements spatiaux :3",
        color=discord.Color.blurple()
    )

    embed.add_field(name="!next", value="Prochain lancement", inline=False)
    embed.add_field(name="!launches [n]", value="Liste des lancements", inline=False)
    embed.add_field(name="!reset", value="Reset admin", inline=False)

    await ctx.send(embed=embed)


# ── RUN ─────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN manquant")
    elif CHANNEL_ID == 0:
        print("❌ CHANNEL_ID manquant")
    else:
        bot.run(TOKEN)
