import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
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

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ───────────────── DB ─────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS announced (
            launch_id TEXT PRIMARY KEY,
            announced_at TEXT
        )
    """)
    con.commit()
    con.close()


def is_announced(launch_id: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT 1 FROM announced WHERE launch_id = ?",
        (launch_id,)
    ).fetchone()
    con.close()
    return row is not None


def mark_announced(launch_id: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR IGNORE INTO announced VALUES (?, datetime('now'))",
        (launch_id,)
    )
    con.commit()
    con.close()


def reset_announced():
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM announced")
    con.commit()
    con.close()


# ───────────────── API ─────────────────

async def fetch_upcoming_launches(limit=10):
    url = "https://lldev.thespacedevs.com/2.3.0/launches/upcoming/"
    params = {"limit": limit, "ordering": "net", "format": "json"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                print("API error:", resp.status)
                return []
            data = await resp.json()
            return data.get("results", [])


# ───────────────── FILTER ─────────────────

def is_future_launch(launch):
    net = launch.get("net")
    if not net:
        return False

    try:
        dt = datetime.fromisoformat(net.replace("Z", "+00:00"))
        return dt > datetime.now(timezone.utc)
    except:
        return False


# ───────────────── EMBED ─────────────────

def format_launch_embed(launch: dict) -> discord.Embed:
    name = launch.get("name", "Lancement inconnu")
    status = launch.get("status", {}).get("name", "Inconnu")
    net = launch.get("net", "")

    date_str = "Date inconnue"

    if net:
        try:
            dt_utc = datetime.fromisoformat(net.replace("Z", "+00:00"))
            dt_paris = dt_utc.astimezone(ZoneInfo("Europe/Paris"))

            mois = [
                "janvier", "février", "mars", "avril", "mai", "juin",
                "juillet", "août", "septembre", "octobre", "novembre", "décembre"
            ]

            date_str = (
                f"{dt_paris.day} {mois[dt_paris.month - 1]} "
                f"{dt_paris.year} à {dt_paris.strftime('%H:%M')} "
                f"(heure de Paris)"
            )
        except:
            date_str = net

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
        color=color_map.get(status, discord.Color.blurple()),
    )

    embed.add_field(name="📅 Date", value=date_str, inline=False)
    embed.add_field(name="📊 Statut", value=status, inline=True)

    rocket = (launch.get("rocket") or {}).get("configuration", {})
    rocket_name = rocket.get("full_name") or rocket.get("name", "Inconnu")
    embed.add_field(name="🛸 Fusée", value=rocket_name, inline=True)

    lsp = (launch.get("launch_service_provider") or {}).get("name", "Inconnu")
    embed.add_field(name="🏢 Opérateur", value=lsp, inline=True)

    pad = launch.get("pad") or {}
    loc = (pad.get("location") or {}).get("name", "")
    pad_name = pad.get("name", "Inconnu")

    embed.add_field(
        name="📍 Site",
        value=f"{pad_name}\n{loc}" if loc else pad_name,
        inline=True
    )

    mission = launch.get("mission")
    if mission:
        if mission.get("type"):
            embed.add_field("🎯 Type", mission["type"], inline=True)

        if mission.get("description"):
            desc = mission["description"][:300]
            embed.add_field("📝 Mission", desc, inline=False)

    image = launch.get("image")
    if isinstance(image, dict):
        embed.set_thumbnail(url=image.get("image_url") or image.get("thumbnail_url"))

    embed.set_footer(text="TheSpaceDevs API")
    return embed


# ───────────────── TASK ─────────────────

@tasks.loop(hours=CHECK_INTERVAL_HOURS)
async def check_launches():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    launches = await fetch_upcoming_launches(15)

    launches = [l for l in launches if is_future_launch(l)]

    count = 0

    for launch in launches:
        lid = launch.get("id")

        if lid and not is_announced(lid):
            await channel.send(embed=format_launch_embed(launch))
            mark_announced(lid)
            count += 1
            await asyncio.sleep(1)

    print(f"{count} nouveaux lancements.")


# ───────────────── EVENTS ─────────────────

@bot.event
async def on_ready():
    init_db()
    print("Bot connecté:", bot.user)

    if not check_launches.is_running():
        check_launches.start()


# ───────────────── COMMANDS ─────────────────

@bot.command()
async def next(ctx):
    launches = await fetch_upcoming_launches(15)
    launches = [l for l in launches if is_future_launch(l)]

    if not launches:
        return await ctx.send("Aucun lancement futur trouvé.")

    launches.sort(key=lambda x: x["net"])

    await ctx.send(embed=format_launch_embed(launches[0]))


@bot.command()
async def launches(ctx, limit: int = 5):
    launches = await fetch_upcoming_launches(limit=10)
    launches = [l for l in launches if is_future_launch(l)]

    for l in launches[:limit]:
        await ctx.send(embed=format_launch_embed(l))
        await asyncio.sleep(0.5)


@bot.command()
@commands.has_permissions(administrator=True)
async def reset(ctx):
    reset_announced()
    await ctx.send("Reset OK.")


# ───────────────── START ─────────────────

if TOKEN and CHANNEL_ID:
    bot.run(TOKEN)
else:
    print("Missing TOKEN or CHANNEL_ID")
