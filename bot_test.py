import discord
import os
import sys
import datetime
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if TOKEN is None:
    print("Error: DISCORD_TOKEN is not set")
    sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

COMMANDS = ["!ping", "!hello", "!time", "!help"]

@client.event
async def on_ready():
    print(f"✅ Bot起動成功: {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content == "!ping":
        await message.channel.send("🏓 Pong! Bot is alive.")

    if message.content == "!hello":
        await message.channel.send(f"こんにちは！{message.author.name}さん")

    if message.content == "!time":
        now = datetime.datetime.now(datetime.timezone.utc)
        await message.channel.send(f"現在時刻 (UTC): {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    if message.content == "!help":
        await message.channel.send(f"使えるコマンド: {', '.join(COMMANDS)}")

client.run(TOKEN)
