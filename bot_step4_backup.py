import discord
import os
import re
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# BotのDiscord IDを保持する変数（起動後に設定）
CLAUDE_BOT_ID = None
GEMINI_BOT_ID = None  # 将来的にGemini BotのIDを設定する場合に使用

# ──────────────────────────────────────────
# メッセージ解析ユーティリティ
# ──────────────────────────────────────────

def parse_message(message: discord.Message) -> dict:
    """
    メッセージを解析してコマンド・テーマ・モードを返す。

    Returns:
        {
            "mentions": list[str],   # メンションされたBotの名前
            "commands": list[str],   # 抽出されたコマンド（/discuss など）
            "turns": int,            # /turns=N の値（デフォルト3）
            "theme": str,            # テーマ（質問内容）
            "mode": int,             # 1=単体質問, 2=同時独立回答, 3=議論
        }
    """
    result = {
        "mentions": [],
        "commands": [],
        "turns": 3,
        "theme": "",
        "mode": 0,
    }

    content = message.content

    # ── 1. @メンション検出 ──
    mentioned_ids = [m.id for m in message.mentions]
    for m in message.mentions:
        result["mentions"].append(m.display_name)

    has_claude = client.user.id in mentioned_ids
    # Gemini BotのIDが設定されている場合はここで判定
    has_gemini = (GEMINI_BOT_ID is not None) and (GEMINI_BOT_ID in mentioned_ids)

    # メンション文字列を除去（解析用に一時除去）
    text = re.sub(r"<@!?\d+>", "", content).strip()

    # ── 2. コマンド抽出（/で始まる単語）──
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
                    result["turns"] = max(1, n)  # 最低1ターン
                except ValueError:
                    pass
            elif cmd_lower == "/continue":
                result["commands"].append("/continue")
            elif cmd_lower == "/stop":
                result["commands"].append("/stop")
            elif cmd_lower in ("/help", "/?"):
                result["commands"].append("/help")
            else:
                # 未知のコマンドはそのまま保持
                result["commands"].append(token)
        else:
            theme_tokens.append(token)

    # ── 3. テーマ抽出 ──
    result["theme"] = " ".join(theme_tokens).strip()

    # ── 4. モード判定 ──
    if has_claude and has_gemini:
        if "/discuss" in result["commands"]:
            result["mode"] = 3  # AI間議論（ターン制）
        else:
            result["mode"] = 2  # 両者への同時質問（独立回答）
    elif has_claude or has_gemini:
        result["mode"] = 1  # 単体質問
    else:
        result["mode"] = 0  # メンションなし（反応しない）

    return result


def format_debug_reply(parsed: dict) -> str:
    """解析結果をDiscord返信用の文字列にフォーマット"""
    mode_labels = {
        0: "なし（メンションなし）",
        1: "モード1：単体質問",
        2: "モード2：両者同時質問（独立回答）",
        3: "モード3：AI間議論（ターン制）",
    }
    commands_str = "、".join(parsed["commands"]) if parsed["commands"] else "（なし）"
    mentions_str = "、".join(parsed["mentions"]) if parsed["mentions"] else "（なし）"
    theme_str = parsed["theme"] if parsed["theme"] else "（なし）"

    lines = [
        "```",
        "📋 メッセージ解析結果（Step 4 デバッグ）",
        "─────────────────────────────",
        f"メンション  : {mentions_str}",
        f"コマンド    : {commands_str}",
        f"ターン数    : {parsed['turns']}",
        f"テーマ      : {theme_str}",
        f"モード      : {mode_labels.get(parsed['mode'], '不明')}",
        "```",
    ]
    return "\n".join(lines)


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
        "`@Claude @Gemini /discuss /turns=5 5Gの未来について`",
    ]
    return "\n".join(lines)


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
    # 自分自身のメッセージは無視
    if message.author == client.user:
        return

    # テスト用コマンド
    if message.content == "!ping":
        await message.channel.send("🏓 Pong! Bot is alive.")
        return

    # 自分（AI-DiscussBot）へのメンションがある場合のみ反応
    if client.user not in message.mentions:
        return

    # /help または /? コマンド
    if "/help" in message.content.lower() or "/?" in message.content:
        await message.channel.send(build_help_message())
        return

    # メッセージ解析してデバッグ結果を返信
    parsed = parse_message(message)
    reply = format_debug_reply(parsed)
    await message.channel.send(reply)


client.run(TOKEN)
