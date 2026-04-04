import asyncio
import discord
import os
import re
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

CLAUDE_BOT_ID = None
GEMINI_BOT_ID = None
DISCORD_MAX_LENGTH = 2000

# ──────────────────────────────────────────
# Claude CLI 呼び出しユーティリティ
# ──────────────────────────────────────────

async def call_claude(prompt: str, timeout: int = 60) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return f"⚠️ Claude CLIがエラーを返しました。\n```\n{err[:500]}\n```"
        return stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return f"⏱️ タイムアウト：Claudeからの応答が{timeout}秒以内に届きませんでした。"
    except FileNotFoundError:
        return "⚠️ `claude` コマンドが見つかりません。"
    except Exception as e:
        return f"⚠️ 予期しないエラーが発生しました：{e}"


# ──────────────────────────────────────────
# Discord メッセージ送信ユーティリティ
# ──────────────────────────────────────────

async def send_long_message(channel: discord.TextChannel, text: str) -> None:
    if len(text) <= DISCORD_MAX_LENGTH:
        await channel.send(text)
        return
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= DISCORD_MAX_LENGTH:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, DISCORD_MAX_LENGTH)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, DISCORD_MAX_LENGTH)
        if split_at == -1:
            split_at = DISCORD_MAX_LENGTH
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    for i, chunk in enumerate(chunks):
        if chunk.strip():
            suffix = f"\n*(続き {i + 1}/{len(chunks)})*" if len(chunks) > 1 and i < len(chunks) - 1 else ""
            await channel.send(chunk + suffix)


# ──────────────────────────────────────────
# メッセージ解析ユーティリティ
# ──────────────────────────────────────────

def parse_message(message: discord.Message) -> dict:
    result = {
        "mentions": [],
        "commands": [],
        "turns": 3,
        "theme": "",
        "mode": 0,
    }
    content = message.content
    mentioned_ids = [m.id for m in message.mentions]
    for m in message.mentions:
        result["mentions"].append(m.display_name)

    has_claude = client.user.id in mentioned_ids
    has_gemini = (GEMINI_BOT_ID is not None) and (GEMINI_BOT_ID in mentioned_ids)

    text = re.sub(r"<@!?\d+>", "", content).strip()
    tokens = text.split()
    theme_tokens = []

    for token in tokens:
        if token.startswith("/"):
            cmd_lower = token.lower()
            if cmd_lower == "/discuss":
                result["commands"].append("/discuss")
            elif cmd_lower.startswith("/turns="):
                result["commands"].append(token)
                try:
                    n = int(token.split("=")[1])
                    result["turns"] = max(1, n)
                except ValueError:
                    pass
            elif cmd_lower == "/continue":
                result["commands"].append("/continue")
            elif cmd_lower == "/stop":
                result["commands"].append("/stop")
            elif cmd_lower in ("/help", "/?"):
                result["commands"].append("/help")
            else:
                result["commands"].append(token)
        else:
            theme_tokens.append(token)

    result["theme"] = " ".join(theme_tokens).strip()

    if has_claude and has_gemini:
        result["mode"] = 3 if "/discuss" in result["commands"] else 2
    elif has_claude or has_gemini:
        result["mode"] = 1
    else:
        result["mode"] = 0

    return result


def build_help_message() -> str:
    lines = [
        "**📖 AI-DiscussBot コマンド一覧**",
        "",
        "`/discuss`　　　　議論モードで起動（両者メンション時のみ有効）",
        "`/turns=N`　　　　議論のターン数を指定（デフォルト: 3）",
        "`/continue`　　　 一時停止後に議論を継続",
        "`/stop`　　　　　 議論を強制終了し両者にまとめを出力",
        "`/help` または `/?`　このヘルプを表示",
        "",
        "**使用例：**",
        "`@AI-DiscussBot 量子コンピュータとは？`　　→ Claude単体に質問",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────
# モード別ハンドラ
# ──────────────────────────────────────────

async def handle_mode1(message: discord.Message, theme: str) -> None:
    if not theme:
        await message.channel.send(
            "⚠️ 質問内容が空です。テーマを入力してください。\n"
            "例：`@AI-DiscussBot 量子コンピュータとは？`"
        )
        return
    await message.channel.send("⏳ Claudeが考え中...")
    response = await call_claude(theme)
    await send_long_message(message.channel, "🟧 **Claude：**\n" + response)


async def handle_mode2(message: discord.Message, theme: str) -> None:
    if not theme:
        await message.channel.send("⚠️ 質問内容が空です。テーマを入力してください。")
        return
    await message.channel.send("⏳ Claudeが考え中...")
    claude_response = await call_claude(theme)
    await send_long_message(message.channel, "🟧 **Claude：**\n" + claude_response)
    await message.channel.send("🟦 **Gemini：**\n⚙️ Gemini連携はStep 6で実装予定です。")


# ──────────────────────────────────────────
# Discordイベントハンドラ
# ──────────────────────────────────────────

@client.event
async def on_ready():
    global CLAUDE_BOT_ID
    CLAUDE_BOT_ID = client.user.id
    print(f"✅ Bot起動成功: {client.user} (ID: {client.user.id})")


@client.event
async def on_message(message: discord.Message):
    # デバッグログ
    print(f"📨 受信: '{message.content[:80]}' / author: {message.author} / mentions: {[m.display_name for m in message.mentions]}")

    if message.author == client.user:
        print("  → 自分のメッセージのため無視")
        return

    if message.content == "!ping":
        await message.channel.send("🏓 Pong! Bot is alive.")
        return

    if client.user not in message.mentions:
        print(f"  → メンションなし（client.user.id={client.user.id}）のため無視")
        return

    print(f"  → メンション確認OK、処理開始")

    if "/help" in message.content.lower() or "/?" in message.content:
        await message.channel.send(build_help_message())
        return

    parsed = parse_message(message)
    theme = parsed["theme"]
    mode = parsed["mode"]
    print(f"  → mode={mode}, theme='{theme}'")

    if mode == 1:
        await handle_mode1(message, theme)
    elif mode == 2:
        await handle_mode2(message, theme)
    elif mode == 3:
        await message.channel.send("⚙️ AI間議論モード（/discuss）はStep 6で実装予定です。")


client.run(TOKEN)
