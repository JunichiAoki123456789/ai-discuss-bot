import discord
import os
import datetime
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if TOKEN == None:
    print("Error: DISCORD_TOKEN is not set")
    exit()

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
        await message.channel.send("こんにちは！" + message.author.name + "さん")

    if message.content == "!time":
        now = datetime.datetime.now()
        await message.channel.send("現在時刻: " + str(now))

    if message.content == "!help":
        await message.channel.send("使えるコマンド: !ping, !hello, !time, !help")

client.run(TOKEN)
