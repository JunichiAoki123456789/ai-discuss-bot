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

DISCORD_MAX_LENGTH = 2000

SUPPORTED_AIS = {"Claude", "Gemini"}
AI_ICON = {"Claude": "🟧", "Gemini": "🟦"}

class BotState:
    INITIAL    = "INITIAL"
    DISCUSSING = "DISCUSSING"
    IDLE       = "IDLE"

bot_state = BotState.INITIAL

last_discussion = {
    "ai_list":  [],
    "turns":    3,
    "theme":    "",
    "history":  [],
    "summary":  "",
}

stop_requested = asyncio.Event()


# ── CLI呼び出し ──────────────────────────────────────────────────
async def call_claude(prompt: str, timeout: int = 60) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return f"⚠️ Claude CLIがエラーを返しました。\n```\n{err[:500]}\n```"
        return stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        return f"⏱️ タイムアウト：Claudeからの応答が{timeout}秒以内に届きませんでした。"
    except FileNotFoundError:
        return "⚠️ `claude` コマンドが見つかりません。"
    except Exception as e:
        return f"⚠️ 予期しないエラーが発生しました：{e}"


async def call_gemini(prompt: str, timeout: int = 60) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", prompt,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return f"⚠️ Gemini CLIがエラーを返しました。\n```\n{err[:500]}\n```"
        return stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        return f"⏱️ タイムアウト：Geminiからの応答が{timeout}秒以内に届きませんでした。"
    except FileNotFoundError:
        return "⚠️ `gemini` コマンドが見つかりません。"
    except Exception as e:
        return f"⚠️ 予期しないエラーが発生しました：{e}"


async def call_ai(ai_name: str, prompt: str, timeout: int = 60) -> str:
    if ai_name == "Claude":
        return await call_claude(prompt, timeout)
    elif ai_name == "Gemini":
        return await call_gemini(prompt, timeout)
    return f"⚠️ 未実装のAIです：{ai_name}"


# ── Discord長文送信 ──────────────────────────────────────────────
async def send_long_message(channel, text: str) -> None:
    if len(text) <= DISCORD_MAX_LENGTH:
        await channel.send(text)
        return
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= DISCORD_MAX_LENGTH:
            chunks.append(remaining); break
        split_at = remaining.rfind("\n", 0, DISCORD_MAX_LENGTH)
        if split_at == -1: split_at = remaining.rfind(" ", 0, DISCORD_MAX_LENGTH)
        if split_at == -1: split_at = DISCORD_MAX_LENGTH
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    for i, chunk in enumerate(chunks):
        if chunk.strip():
            suffix = f"\n*(続き {i+1}/{len(chunks)})*" if len(chunks) > 1 and i < len(chunks)-1 else ""
            await channel.send(chunk + suffix)


# ── /AI=パーサー ─────────────────────────────────────────────────
def parse_ai_command(token: str, rest_tokens: list) -> tuple:
    raw = token[len("/AI="):]
    if raw == "":
        return [], 0, "⚠️ `/AI=` にAI名を指定してください。例：`/AI=Claude` または `/AI=(Claude,Gemini)`"
    if raw.startswith("("):
        combined = raw
        consumed = 0
        while ")" not in combined and consumed < len(rest_tokens):
            combined += rest_tokens[consumed]; consumed += 1
        if ")" not in combined:
            return [], consumed, "⚠️ `/AI=` のカッコが閉じていません。例：`/AI=(Claude,Gemini)`"
        inner = combined[1:combined.index(")")]
        if inner.strip() == "":
            return [], consumed, "⚠️ `/AI=()` の中にAI名を指定してください。"
        names_raw = [n.strip() for n in inner.split(",")]
        if any(n == "" for n in names_raw):
            return [], consumed, "⚠️ `/AI=` の中に空の要素があります。例：`/AI=(Claude,Gemini)`"
        return _validate_ai_names(names_raw), consumed, _ai_names_error(names_raw)
    else:
        if "," in raw:
            return [], 0, "⚠️ 複数AIを指定する場合はカッコが必要です。例：`/AI=(Claude,Gemini)`"
        return _validate_ai_names([raw]), 0, _ai_names_error([raw])

def _normalize_ai_name(name): return name.capitalize()
def _validate_ai_names(names_raw): return [_normalize_ai_name(n) for n in names_raw]
def _ai_names_error(names_raw):
    normalized = [_normalize_ai_name(n) for n in names_raw]
    unsupported = [n for n in normalized if n not in SUPPORTED_AIS]
    if unsupported:
        return f"⚠️ `{'、'.join(unsupported)}` は対応していないAIです。現在対応中のAI：Claude、Gemini"
    if len(normalized) != len(set(normalized)):
        return "⚠️ 同じAIが重複しています。各AIは1回のみ指定してください。"
    return None


# ── メッセージパーサー ───────────────────────────────────────────
def parse_message(content: str) -> dict:
    result = {"ai_list": ["Claude"], "commands": [], "turns": 3,
              "theme": "", "errors": [], "unknown_cmds": []}
    text = re.sub(r"<@!?\d+>", "", content).strip()
    tokens = text.split()
    theme_tokens = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("/"):
            theme_tokens.append(token); i += 1; continue
        cmd_lower = token.lower()
        if cmd_lower.startswith("/ai="):
            ai_list, consumed, err = parse_ai_command(token, tokens[i+1:])
            i += 1 + consumed
            if err: result["errors"].append(err)
            else:   result["ai_list"] = ai_list
            continue
        if cmd_lower == "/discuss":
            result["commands"].append("/discuss"); i += 1; continue
        if cmd_lower.startswith("/turns="):
            val = token[len("/turns="):]
            if val == "":
                result["errors"].append("⚠️ `/turns` に値を指定してください。例：`/turns=3`")
            else:
                try:
                    n = int(val)
                    if n <= 0: result["errors"].append("⚠️ `/turns` は1以上の整数を指定してください。例：`/turns=3`")
                    else:      result["turns"] = n; result["commands"].append(token)
                except ValueError:
                    result["errors"].append("⚠️ `/turns` の値が正しくありません。例：`/turns=3`")
            i += 1; continue
        if cmd_lower == "/continue": result["commands"].append("/continue"); i += 1; continue
        if cmd_lower == "/stop":     result["commands"].append("/stop");     i += 1; continue
        if cmd_lower == "/summary":  result["commands"].append("/summary");  i += 1; continue
        if cmd_lower in ("/help", "/?"):
            result["commands"].append("/help"); i += 1; continue
        result["unknown_cmds"].append(token); i += 1
    result["theme"] = " ".join(theme_tokens).strip()
    return result


# ── 議論プロンプト構築 ───────────────────────────────────────────
def build_discuss_prompt(ai_name: str, theme: str, history: list) -> str:
    lines = [
        f"あなたは {ai_name} です。以下のテーマについて、他のAIと議論してください。",
        f"テーマ：{theme}", "",
        "【これまでの議論】",
    ]
    if not history:
        lines.append("（まだ発言はありません。最初の発言をしてください）")
    else:
        for entry in history:
            icon = AI_ICON.get(entry["ai"], "🤖")
            lines.append(f"{icon} {entry['ai']}：{entry['text']}")
    lines += [
        "",
        f"あなた（{ai_name}）の番です。上記の議論を踏まえて、簡潔に返答してください。",
        "（返答のみを出力してください。前置きや「承知しました」などは不要です）",
    ]
    return "\n".join(lines)


def build_summary_prompt(theme: str, history: list, ai_list: list) -> str:
    lines = [
        "以下のAI間の議論を簡潔にサマリーしてください。",
        f"テーマ：{theme}", "",
        "【議論の全内容】",
    ]
    for entry in history:
        icon = AI_ICON.get(entry["ai"], "🤖")
        lines.append(f"{icon} {entry['ai']}：{entry['text']}")
    lines += ["", "【出力形式】", "以下の形式で出力してください："]
    for ai in ai_list:
        lines.append(f"{AI_ICON.get(ai,'🤖')} {ai} の主張：")
        lines.append("・[主張の要約を箇条書きで2〜3点]")
    lines.append("\n※ 出力は上記形式のみ。前置き・後書き不要。")
    return "\n".join(lines)


def format_summary(theme: str, turns_done: int, ai_list: list, summary_text: str) -> str:
    ai_str = "・".join([f"{AI_ICON.get(a,'🤖')}{a}" for a in ai_list])
    return (
        f"📝 **議論サマリー**\n"
        f"テーマ：{theme}　／　ターン数：{turns_done}ターン完了\n"
        f"参加AI：{ai_str}\n\n"
        f"{summary_text}\n\n"
        f"続ける場合 → `@AI-DiscussBot /continue`\n"
        f"新しい議論 → `@AI-DiscussBot /discuss /AI=({','.join(ai_list)}) [新テーマ]`"
    )


# ── 議論ループ ────────────────────────────────────────────────────
async def run_discussion_loop(channel, ai_list, turns, theme, history) -> int:
    turns_done = 0
    for turn_no in range(1, turns + 1):
        await channel.send(f"━━━━━━━━━━ **ターン {turn_no} / {turns}** ━━━━━━━━━━")
        for ai_name in ai_list:
            icon = AI_ICON.get(ai_name, "🤖")
            prompt = build_discuss_prompt(ai_name, theme, history)
            await channel.send(f"⏳ {ai_name}が考え中...")
            response = await call_ai(ai_name, prompt)
            history.append({"ai": ai_name, "text": response})
            await send_long_message(channel, f"{icon} **{ai_name}：**\n{response}")
        turns_done += 1
        if stop_requested.is_set():
            await channel.send("🛑 `/stop` により議論を終了します。")
            break
    return turns_done


async def generate_and_post_summary(channel, theme, turns_done, ai_list, history) -> str:
    await channel.send("📝 サマリーを生成中...")
    summary_prompt = build_summary_prompt(theme, history, ai_list)
    raw_summary = await call_ai(ai_list[0], summary_prompt)
    full_summary = format_summary(theme, turns_done, ai_list, raw_summary)
    await send_long_message(channel, full_summary)
    return full_summary


async def handle_discuss(channel, ai_list, turns, theme) -> None:
    global bot_state, last_discussion, stop_requested
    bot_state = BotState.DISCUSSING
    stop_requested.clear()
    history = []
    last_discussion.update({"ai_list": ai_list, "turns": turns, "theme": theme, "history": history})
    ai_names = "・".join([f"{AI_ICON.get(a,'🤖')}{a}" for a in ai_list])
    await channel.send(
        f"🗣️ **議論を開始します**\n"
        f"テーマ：{theme}\n"
        f"参加AI：{ai_names}　／　ターン数：{turns}\n"
        f"議論中に停止する場合は `@AI-DiscussBot /stop`"
    )
    turns_done = await run_discussion_loop(channel, ai_list, turns, theme, history)
    summary = await generate_and_post_summary(channel, theme, turns_done, ai_list, history)
    last_discussion["summary"] = summary
    bot_state = BotState.IDLE


async def handle_continue(channel, turns) -> None:
    global bot_state, last_discussion, stop_requested
    ai_list = last_discussion["ai_list"]
    theme   = last_discussion["theme"]
    history = last_discussion["history"]
    bot_state = BotState.DISCUSSING
    stop_requested.clear()
    ai_names = "・".join([f"{AI_ICON.get(a,'🤖')}{a}" for a in ai_list])
    await channel.send(
        f"▶️ **議論を継続します**\n"
        f"テーマ：{theme}\n"
        f"参加AI：{ai_names}　／　追加ターン数：{turns}"
    )
    turns_done = await run_discussion_loop(channel, ai_list, turns, theme, history)
    summary = await generate_and_post_summary(channel, theme, turns_done, ai_list, history)
    last_discussion["summary"] = summary
    bot_state = BotState.IDLE


# ── モード1・2 ────────────────────────────────────────────────────
async def handle_single_ai(channel, ai_name, theme) -> None:
    icon = AI_ICON.get(ai_name, "🤖")
    await channel.send(f"⏳ {ai_name}が考え中...")
    response = await call_ai(ai_name, theme)
    await send_long_message(channel, f"{icon} **{ai_name}：**\n{response}")


async def handle_multi_ai(channel, ai_list, theme) -> None:
    for ai_name in ai_list:
        icon = AI_ICON.get(ai_name, "🤖")
        await channel.send(f"⏳ {ai_name}が考え中...")
        response = await call_ai(ai_name, theme)
        await send_long_message(channel, f"{icon} **{ai_name}：**\n{response}")


# ── ヘルプ ───────────────────────────────────────────────────────
def build_help_message() -> str:
    return "\n".join([
        "**📖 AI-DiscussBot コマンド一覧**", "",
        "`@AI-DiscussBot [質問]`　　　　　　　→ Claudeのみ回答（デフォルト）",
        "`@AI-DiscussBot /AI=Claude [質問]`　→ Claudeのみ回答",
        "`@AI-DiscussBot /AI=Gemini [質問]`　→ Geminiのみ回答",
        "`@AI-DiscussBot /AI=(Claude,Gemini) [質問]`　→ 両者が独立回答", "",
        "**議論モード：**",
        "`/discuss /AI=(Claude,Gemini) [テーマ]`　→ ターン制議論（デフォルト3ターン）",
        "`/discuss /AI=(Claude,Gemini) /turns=N [テーマ]`　→ ターン数指定",
        "`/continue`　→ 直前の議論を継続（デフォルト3ターン）",
        "`/continue /turns=N`　→ 指定ターン数で継続",
        "`/stop`　→ 議論を強制終了しサマリー出力",
        "`/summary`　→ 直前の議論サマリーを再表示", "",
        "`/help` または `/?`　→ このヘルプを表示", "",
        "**使用例：**",
        "`@AI-DiscussBot 量子コンピュータとは？`",
        "`@AI-DiscussBot /AI=(Claude,Gemini) AIの未来について教えて`",
        "`@AI-DiscussBot /discuss /AI=(Claude,Gemini) /turns=2 5Gの未来`",
    ])


# ── on_message ───────────────────────────────────────────────────
@client.event
async def on_ready():
    print(f"✅ Bot起動成功: {client.user} (ID: {client.user.id})")


@client.event
async def on_message(message: discord.Message):
    print(f"📨 受信: '{message.content[:80]}' / author: {message.author}")
    if message.author == client.user:
        return
    if message.content == "!ping":
        await message.channel.send("🏓 Pong! Bot is alive.")
        return
    if client.user not in message.mentions:
        print(f"  → メンションなし（client.user.id={client.user.id}）のため無視")
        return
    print(f"  → メンション確認OK、処理開始")

    text_no_mention = re.sub(r"<@!?\d+>", "", message.content).strip()
    if "/help" in text_no_mention.lower() or "/?" in text_no_mention:
        await message.channel.send(build_help_message())
        return

    parsed = parse_message(message.content)
    print(f"  → parsed: ai_list={parsed['ai_list']}, commands={parsed['commands']}, "
          f"turns={parsed['turns']}, theme='{parsed['theme']}', "
          f"errors={parsed['errors']}, unknown_cmds={parsed['unknown_cmds']}")

    if parsed["unknown_cmds"]:
        for unk in parsed["unknown_cmds"]:
            await message.channel.send(
                f"⚠️ 不明なコマンド `{unk}` です。`/help` でコマンド一覧を確認してください。")
        return
    if parsed["errors"]:
        for err in parsed["errors"]:
            await message.channel.send(err)
        return

    commands = parsed["commands"]
    ai_list  = parsed["ai_list"]
    theme    = parsed["theme"]

    # /discuss
    if "/discuss" in commands:
        if bot_state == BotState.DISCUSSING:
            await message.channel.send(
                "⚠️ 現在議論が進行中です。`/stop` で終了してから新しい議論を開始してください。")
            return
        if len(ai_list) < 2:
            await message.channel.send(
                "⚠️ `/discuss` は2種類以上のAIを指定してください。例：`/discuss /AI=(Claude,Gemini) テーマ`")
            return
        if not theme:
            await message.channel.send(
                "⚠️ テーマを入力してください。例：`@AI-DiscussBot /discuss /AI=(Claude,Gemini) 5Gの未来`")
            return
        await handle_discuss(message.channel, ai_list, parsed["turns"], theme)
        return

    # /continue
    if "/continue" in commands:
        if bot_state == BotState.INITIAL:
            await message.channel.send(
                "⚠️ 議論履歴がありません。まず `/discuss /AI=(Claude,Gemini) テーマ` で議論を開始してください。")
            return
        if bot_state == BotState.DISCUSSING:
            await message.channel.send("⚠️ 議論は現在進行中です。終了するまでお待ちください。")
            return
        await handle_continue(message.channel, parsed["turns"])
        return

    # /stop
    if "/stop" in commands:
        if bot_state != BotState.DISCUSSING:
            await message.channel.send("⚠️ 現在進行中の議論がありません。")
            return
        stop_requested.set()
        await message.channel.send("🛑 停止リクエストを受け付けました。現在のターン完了後に停止します。")
        return

    # /summary
    if "/summary" in commands:
        if bot_state == BotState.INITIAL:
            await message.channel.send("⚠️ 議論履歴がありません。まず `/discuss` で議論を開始してください。")
            return
        if bot_state == BotState.DISCUSSING:
            await message.channel.send("⚠️ 議論が完了すると自動でサマリーが出力されます。完了までお待ちください。")
            return
        if last_discussion["summary"]:
            await send_long_message(message.channel, last_discussion["summary"])
        else:
            await message.channel.send("⚠️ 表示できるサマリーがありません。")
        return

    # モード1・2（DISCUSSING中の単体質問も状態変化なしで回答 No.14）
    if not theme:
        await message.channel.send("⚠️ 質問内容が空です。テーマを入力してください。")
        return
    if len(ai_list) == 1:
        await handle_single_ai(message.channel, ai_list[0], theme)
    else:
        await handle_multi_ai(message.channel, ai_list, theme)


client.run(TOKEN)
