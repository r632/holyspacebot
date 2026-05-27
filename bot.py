import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import sqlite3
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", "6"))

# Base SQLite — sur Railway, monte un volume sur /data pour la persistance
# Sinon ça reste en mémoire entre les redémarrages (mais Railway redémarre rarement)
DB_PATH = os.getenv("DB_PATH", "/data/launches.db" if os.path.isdir("/data") else "launches.db")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ── Base de données ────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS announced (
            launch_id    TEXT PRIMARY KEY,
            announced_at TEXT
        )
    """)
    con.commit()
    con.close()


def is_announced(launch_id: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT 1 FROM announced WHERE launch_id = ?", (launch_id,)).fetchone()
    con.close()
    return row is not None


def mark_announced(launch_id: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR IGNORE INTO announced (launch_id, announced_at) VALUES (?, datetime('now'))",
        (launch_id,)
    )
    con.commit()
    con.close()


def reset_announced():
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM announced")
    con.commit()
    con.close()


def count_announced() -> int:
    con = sqlite3.connect(DB_PATH)
    n = con.execute("SELECT COUNT(*) FROM announced").fetchone()[0]
    con.close()
    return n


# ── API ────────────────────────────────────────────────────────────────────────

async def fetch_upcoming_launches(limit=5):
    """Récupère les prochains lancements depuis l'API TheSpaceDevs (qui alimente NextSpaceFlight).

    lldev.thespacedevs.com = endpoint gratuit (15 req/heure, données légèrement décalées)
    ll.thespacedevs.com    = endpoint production (token Patreon pour gros volumes)
    """
    url = "https://lldev.thespacedevs.com/2.3.0/launches/upcoming/"
    params = {"limit": limit, "ordering": "net", "format": "json"}
    headers = {"User-Agent": "DiscordSpaceLaunchBot/1.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("results", [])
            print(f"Erreur API: {resp.status}")
            return []


# ── Embeds ─────────────────────────────────────────────────────────────────────

def format_launch_embed(launch: dict) -> discord.Embed:
    name   = launch.get("name", "Lancement inconnu")
    status = launch.get("status", {}).get("name", "Inconnu")
    net    = launch.get("net", "")

    date_str = "Date inconnue"
    if net:
        try:
            dt = datetime.fromisoformat(net.replace("Z", "+00:00"))
            date_str = f"<t:{int(dt.timestamp())}:F>"
        except Exception:
            date_str = net

    color_map = {
        "Go for Launch": discord.Color.green(),
        "TBD":           discord.Color.orange(),
        "TBC":           discord.Color.gold(),
        "Success":       discord.Color.blue(),
        "Failure":       discord.Color.red(),
        "Hold":          discord.Color.red(),
        "In Flight":     discord.Color.teal(),
    }
    color = color_map.get(status, discord.Color.blurple())

    slug = launch.get("slug", "")
    nsf_url = f"https://nextspaceflight.com/launches?search={slug}" if slug else "https://nextspaceflight.com/launches"

    embed = discord.Embed(
        title=f"🚀 {name}",
        color=color,
        url=nsf_url,
    )
    embed.add_field(name="📅 Date de lancement", value=date_str, inline=False)
    embed.add_field(name="📊 Statut", value=status, inline=True)

    rocket_name = (launch.get("rocket") or {}).get("configuration", {}).get("full_name") \
               or (launch.get("rocket") or {}).get("configuration", {}).get("name", "Inconnu")
    embed.add_field(name="🛸 Fusée", value=rocket_name, inline=True)

    lsp_name = (launch.get("launch_service_provider") or {}).get("name", "Inconnu")
    embed.add_field(name="🏢 Opérateur", value=lsp_name, inline=True)

    pad      = launch.get("pad") or {}
    location = pad.get("location") or {}
    pad_name = pad.get("name", "Inconnu")
    loc_name = location.get("name", "")
    embed.add_field(name="📍 Site", value=f"{pad_name}\n{loc_name}" if loc_name else pad_name, inline=True)

    mission = launch.get("mission")
    if mission:
        mtype = mission.get("type", "")
        mdesc = mission.get("description", "")
        if mtype:
            embed.add_field(name="🎯 Type de mission", value=mtype, inline=True)
        if mdesc:
            embed.add_field(name="📝 Description", value=mdesc[:300] + ("..." if len(mdesc) > 300 else ""), inline=False)

    image = launch.get("image")
    image_url = (image.get("image_url") or image.get("thumbnail_url")) if isinstance(image, dict) else (image if isinstance(image, str) else None)
    if image_url:
        embed.set_thumbnail(url=image_url)

    embed.set_footer(text="Données : TheSpaceDevs / NextSpaceFlight.com")
    return embed


# ── Tâche périodique ───────────────────────────────────────────────────────────

@tasks.loop(hours=CHECK_INTERVAL_HOURS)
async def check_launches():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"Canal introuvable (ID: {CHANNEL_ID})")
        return

    launches  = await fetch_upcoming_launches(limit=10)
    new_count = 0

    for launch in launches:
        launch_id = launch.get("id")
        if launch_id and not is_announced(launch_id):
            await channel.send(embed=format_launch_embed(launch))
            mark_announced(launch_id)
            new_count += 1
            await asyncio.sleep(1)

    print(f"{new_count} nouveau(x) lancement(s) annoncé(s)." if new_count else "Aucun nouveau lancement.")


# ── Événements ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    init_db()
    print(f"✅ Bot connecté : {bot.user}")
    print(f"📡 Canal cible  : {CHANNEL_ID}")
    print(f"🗄️  Base SQLite  : {DB_PATH} ({count_announced()} lancement(s) déjà annoncé(s))")
    if not check_launches.is_running():
        check_launches.start()


# ── Commandes ──────────────────────────────────────────────────────────────────

@bot.command(name="launches", aliases=["lancements"])
async def cmd_launches(ctx, limit: int = 5):
    """!launches [nombre] — Affiche les prochains lancements."""
    limit = max(1, min(limit, 10))
    await ctx.send(f"🔭 Récupération des {limit} prochains lancements...")
    launches = await fetch_upcoming_launches(limit=limit)
    if not launches:
        await ctx.send("❌ Impossible de récupérer les lancements pour le moment.")
        return
    for launch in launches:
        await ctx.send(embed=format_launch_embed(launch))
        await asyncio.sleep(0.5)


@bot.command(name="next", aliases=["prochain"])
async def cmd_next(ctx):
    """!next — Affiche le prochain lancement."""
    launches = await fetch_upcoming_launches(limit=1)
    if not launches:
        await ctx.send("❌ Impossible de récupérer le lancement pour le moment.")
        return
    await ctx.send(embed=format_launch_embed(launches[0]))


@bot.command(name="reset")
@commands.has_permissions(administrator=True)
async def cmd_reset(ctx):
    """!reset — (Admin) Remet à zéro la liste des lancements annoncés."""
    reset_announced()
    await ctx.send("✅ Liste des lancements annoncés réinitialisée.")


@bot.command(name="spacehelp")
async def cmd_help(ctx):
    embed = discord.Embed(
        title="🚀 Space Launch Bot — Aide",
        description="Bot d'annonce des prochains lancements spatiaux (données : NextSpaceFlight / TheSpaceDevs)",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="!next / !prochain",          value="Affiche le prochain lancement",                          inline=False)
    embed.add_field(name="!launches [n] / !lancements", value="Affiche les n prochains lancements (max 10, défaut 5)", inline=False)
    embed.add_field(name="!reset",                     value="(Admin) Réinitialise les annonces",                     inline=False)
    embed.set_footer(text=f"Vérification automatique toutes les {CHECK_INTERVAL_HOURS}h")
    await ctx.send(embed=embed)


# ── Lancement ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN manquant dans le fichier .env")
    elif CHANNEL_ID == 0:
        print("❌ CHANNEL_ID manquant dans le fichier .env")
    else:
        bot.run(TOKEN)
