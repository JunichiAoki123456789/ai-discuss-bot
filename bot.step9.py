"""
AI-DiscussBot  - bot.py  Step 8
エラーハンドリング全25件 完全実装版
"""

import asyncio
import os
import re
import subprocess
from enum import Enum, auto

import discord
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ─────────────────────────────────────────
#  定数
# ─────────────────────────────────────────
SUPPORTED_AIS = {"claude", "gemini"}          # 小文字で管理
AI_DISPLAY = {"claude": "Claude", "gemini": "Gemini"}
AI_EMOJI   = {"claude": "🟧", "gemini": "🟦"}
DEFAULT_TURNS = 3
MAX_DISCORD_LEN = 1900                         # 分割送信のしきい値（2000より少し余裕を持たせる）


# ─────────────────────────────────────────
#  Bot状態
# ─────────────────────────────────────────
class BotState(Enum):
    INITIAL    = auto()   # 起動後、一度もdiscussしていない
    DISCUSSING = auto()   # ターン実行中
    IDLE       = auto()   # ターン完了後 or /stop後


# ─────────────────────────────────────────
#  グローバル状態
# ─────────────────────────────────────────
bot_state = BotState.INITIAL

last_discussion = {
    "ai_list": [],      # 例: ["gemini", "claude"]  （発言順）
    "turns": 0,
    "theme": "",
    "history": [],      # {"ai": "claude", "text": "..."} のリスト
    "summary": "",
}

stop_requested = asyncio.Event()


# ─────────────────────────────────────────
#  Discord クライアント
# ─────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ─────────────────────────────────────────
#  CLI 呼び出し
# ─────────────────────────────────────────
def call_claude(prompt: str) -> str:
    print(f"[DEBUG] call_claude: {prompt[:80]!r}...")
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=120
    )
    return result.stdout.strip() or result.stderr.strip() or "(Claude から応答なし)"


def call_gemini(prompt: str) -> str:
    print(f"[DEBUG] call_gemini: {prompt[:80]!r}...")
    result = subprocess.run(
        ["gemini", "-p", prompt],
        capture_output=True, text=True, timeout=120
    )
    return result.stdout.strip() or result.stderr.strip() or "(Gemini から応答なし)"


def call_ai(ai_name: str, prompt: str) -> str:
    """ai_name は小文字"""
    if ai_name == "claude":
        return call_claude(prompt)
    elif ai_name == "gemini":
        return call_gemini(prompt)
    else:
        return f"({ai_name} は未実装)"


# ─────────────────────────────────────────
#  Discord 長文分割送信
# ─────────────────────────────────────────
async def send_long(channel, text: str):
    """2000文字制限対応：改行・スペース優先で分割"""
    while len(text) > MAX_DISCORD_LEN:
        # 改行で分割を試みる
        idx = text.rfind("\n", 0, MAX_DISCORD_LEN)
        if idx == -1:
            idx = text.rfind(" ", 0, MAX_DISCORD_LEN)
        if idx == -1:
            idx = MAX_DISCORD_LEN
        await channel.send(text[:idx])
        text = text[idx:].lstrip("\n")
    if text:
        await channel.send(text)


# ─────────────────────────────────────────
#  /AI= パーサー
#  戻り値: (ai_list: list[str], error_msg: str | None)
#  ai_list は小文字
# ─────────────────────────────────────────
def parse_ai_command(ai_token: str):
    """
    ai_token: "/AI=" の後ろの部分（例: "Claude"  "(Claude,Gemini)"  ""  "()" ...）
    """
    raw = ai_token  # 元の文字列を保持（エラーメッセージ用）

    # No.15: /AI= のみ（値なし）
    if raw == "":
        return [], "⚠️ /AI= にAI名を指定してください。例：/AI=Claude または /AI=(Claude,Gemini)"

    # カッコあり記法
    if raw.startswith("("):
        # No.17: 閉じカッコなし
        if not raw.endswith(")"):
            return [], "⚠️ /AI= のカッコが閉じていません。例：/AI=(Claude,Gemini)"

        inner = raw[1:-1].strip()

        # No.16: /AI=()
        if inner == "":
            return [], "⚠️ /AI=() の中にAI名を指定してください。"

        parts = [p.strip() for p in inner.split(",")]

        # No.19: 空の要素（例: "Gemini,,Claude"）
        if any(p == "" for p in parts):
            return [], "⚠️ /AI= の中に空の要素があります。例：/AI=(Claude,Gemini)"

        ai_list_lower = [p.lower() for p in parts]

        # No.20: 未対応AI名
        for i, name in enumerate(ai_list_lower):
            if name not in SUPPORTED_AIS:
                return [], f"⚠️ {parts[i]} は対応していないAIです。現在対応中のAI：Claude、Gemini"

        # No.21: 重複
        if len(ai_list_lower) != len(set(ai_list_lower)):
            return [], "⚠️ 同じAIが重複しています。各AIは1回のみ指定してください。"

        return ai_list_lower, None

    else:
        # カッコなし記法
        # No.18: カッコなしで複数指定（カンマを含む）
        if "," in raw:
            return [], "⚠️ 複数AIを指定する場合はカッコが必要です。例：/AI=(Claude,Gemini)"

        name_lower = raw.lower()

        # No.20: 未対応AI名
        if name_lower not in SUPPORTED_AIS:
            return [], f"⚠️ {raw} は対応していないAIです。現在対応中のAI：Claude、Gemini"

        return [name_lower], None


# ─────────────────────────────────────────
#  /turns= パーサー
#  戻り値: (turns: int | None, error_msg: str | None)
# ─────────────────────────────────────────
def parse_turns(turns_token: str):
    """
    turns_token: "/turns=" の後ろの部分（例: "3"  "0"  "abc"  ""）
    """
    # No.24: 値なし
    if turns_token == "":
        return None, "⚠️ /turns に値を指定してください。例：/turns=3"

    # No.23: 数字以外
    if not turns_token.lstrip("-").isdigit():
        return None, "⚠️ /turns の値が正しくありません。例：/turns=3"

    val = int(turns_token)

    # No.22: 0以下
    if val <= 0:
        return None, "⚠️ /turns は1以上の整数を指定してください。例：/turns=3"

    return val, None


# ─────────────────────────────────────────
#  メッセージ全体のパーサー
# ─────────────────────────────────────────
def parse_message(content: str):
    """
    メンション除去済みのテキストを受け取り、コマンド情報を返す。

    戻り値:
        {
          "has_discuss": bool,
          "has_continue": bool,
          "has_stop": bool,
          "has_summary": bool,
          "has_help": bool,
          "ai_list": list[str] | None,   # Noneは/AI=未指定
          "turns": int | None,            # Noneは/turns=未指定
          "theme": str,                   # コマンドを除いた残りテキスト
          "unknown_commands": list[str],  # 未知の /xxx コマンド
          "error_msg": str | None,        # パースエラーメッセージ
        }
    """
    result = {
        "has_discuss":  False,
        "has_continue": False,
        "has_stop":     False,
        "has_summary":  False,
        "has_help":     False,
        "ai_list":      None,
        "turns":        None,
        "theme":        "",
        "unknown_commands": [],
        "error_msg":    None,
    }

    tokens = content.split()
    remaining = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        tok_lower = tok.lower()

        if tok_lower == "/discuss":
            result["has_discuss"] = True

        elif tok_lower == "/continue":
            result["has_continue"] = True

        elif tok_lower == "/stop":
            result["has_stop"] = True

        elif tok_lower == "/summary":
            result["has_summary"] = True

        elif tok_lower in ("/help", "/?"):
            result["has_help"] = True

        elif re.match(r"^/ai=", tok, re.IGNORECASE):
            ai_val = tok[4:]  # "/AI=" の後ろ
            ai_list, err = parse_ai_command(ai_val)
            if err:
                result["error_msg"] = err
                return result
            result["ai_list"] = ai_list

        elif re.match(r"^/turns=", tok, re.IGNORECASE):
            turns_val = tok[7:]  # "/turns=" の後ろ
            turns, err = parse_turns(turns_val)
            if err:
                result["error_msg"] = err
                return result
            result["turns"] = turns

        elif tok.startswith("/"):
            result["unknown_commands"].append(tok)

        else:
            remaining.append(tok)

        i += 1

    result["theme"] = " ".join(remaining).strip()
    return result


# ─────────────────────────────────────────
#  議論ループ
# ─────────────────────────────────────────
async def run_discussion_loop(channel, ai_list: list, turns: int, theme: str, history: list):
    global bot_state, last_discussion

    stop_requested.clear()

    for t in range(1, turns + 1):
        if stop_requested.is_set():
            await channel.send(f"⏹️ /stop を受け付けました。ターン {t - 1} で議論を終了します。")
            break

        await channel.send(f"━━━ ターン {t} / {turns} ━━━")

        for ai in ai_list:
            if stop_requested.is_set():
                break

            # プロンプト構築
            if not history:
                prompt = (
                    f"テーマ「{theme}」について、あなたの立場・意見を述べてください（日本語で）。"
                )
            else:
                prev_text = "\n".join(
                    f"{AI_DISPLAY.get(h['ai'], h['ai'])}：{h['text']}" for h in history
                )
                prompt = (
                    f"テーマ「{theme}」についての議論。\n"
                    f"これまでの発言：\n{prev_text}\n\n"
                    f"あなた（{AI_DISPLAY.get(ai, ai)}）の次の発言を述べてください（日本語で）。"
                )

            await channel.send(f"{AI_EMOJI.get(ai, '🤖')} **{AI_DISPLAY.get(ai, ai)}** が考え中…")

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, call_ai, ai, prompt)

            header = f"{AI_EMOJI.get(ai, '🤖')} **{AI_DISPLAY.get(ai, ai)}**："
            await send_long(channel, header + "\n" + response)

            history.append({"ai": ai, "text": response})

    # 状態更新
    last_discussion["history"] = history
    last_discussion["turns"]   = t if stop_requested.is_set() else turns
    bot_state = BotState.IDLE

    # サマリー自動出力
    await generate_and_post_summary(channel)


# ─────────────────────────────────────────
#  サマリー生成・投稿
# ─────────────────────────────────────────
async def generate_and_post_summary(channel):
    global last_discussion

    theme   = last_discussion["theme"]
    history = last_discussion["history"]
    turns   = last_discussion["turns"]
    ai_list = last_discussion["ai_list"]

    # 各AIの発言をまとめてサマリーをClaudeに生成させる
    transcript = "\n".join(
        f"{AI_DISPLAY.get(h['ai'], h['ai'])}：{h['text']}" for h in history
    )
    summary_prompt = (
        f"以下はテーマ「{theme}」についてのAI同士の議論です。\n\n"
        f"{transcript}\n\n"
        f"各AIの主張を簡潔に日本語で箇条書きでまとめてください。"
    )

    await channel.send("📝 サマリーを生成中…")
    loop = asyncio.get_event_loop()
    summary_text = await loop.run_in_executor(None, call_claude, summary_prompt)
    last_discussion["summary"] = summary_text

    # 出力フォーマット（設計書 4.4）
    lines = [f"📝 **議論サマリー**", f"テーマ：{theme}　／　ターン数：{turns}ターン完了", ""]
    for ai in ai_list:
        lines.append(f"{AI_EMOJI.get(ai, '🤖')} **{AI_DISPLAY.get(ai, ai)} の主張：**")
    lines.append("")
    lines.append(summary_text)
    lines.append("")
    lines.append("続ける場合 → `@AI-DiscussBot /continue`")
    lines.append("新しい議論 → `@AI-DiscussBot /discuss /AI=(Claude,Gemini) [新テーマ]`")

    await send_long(channel, "\n".join(lines))


# ─────────────────────────────────────────
#  ヘルプ
# ─────────────────────────────────────────
HELP_TEXT = """\
📖 **AI-DiscussBot コマンド一覧**

**単体質問**
`@AI-DiscussBot [質問]`　→ Claudeが回答
`@AI-DiscussBot /AI=Claude [質問]`　→ Claudeが回答
`@AI-DiscussBot /AI=Gemini [質問]`　→ Geminiが回答

**複数AI独立回答**
`@AI-DiscussBot /AI=(Claude,Gemini) [質問]`

**議論モード**
`@AI-DiscussBot /discuss /AI=(Claude,Gemini) [テーマ]`
`@AI-DiscussBot /discuss /AI=(Claude,Gemini) /turns=5 [テーマ]`

**議論制御**
`@AI-DiscussBot /continue`　→ 直前の議論を継続（デフォルト3ターン）
`@AI-DiscussBot /continue /turns=N`　→ Nターンで継続
`@AI-DiscussBot /stop`　→ 現在のターン完了後に停止・サマリー出力
`@AI-DiscussBot /summary`　→ 直前のサマリーを再表示

**その他**
`@AI-DiscussBot /help` または `/?`　→ このヘルプを表示
"""


# ─────────────────────────────────────────
#  コマンドハンドラ群
# ─────────────────────────────────────────
async def handle_help(channel):
    await channel.send(HELP_TEXT)


async def handle_stop(channel):
    global bot_state
    # No.1・2: INITIAL / IDLE での /stop
    if bot_state != BotState.DISCUSSING:
        await channel.send("⚠️ 現在進行中の議論がありません。")
        return
    # No.3: DISCUSSING での /stop → 正常
    stop_requested.set()
    await channel.send("⏹️ /stop を受け付けました。現在のターン完了後に停止します。")


async def handle_summary(channel):
    global bot_state
    # No.7: INITIAL での /summary
    if bot_state == BotState.INITIAL:
        await channel.send("⚠️ 議論履歴がありません。まず /discuss で議論を開始してください。")
        return
    # No.8: DISCUSSING での /summary
    if bot_state == BotState.DISCUSSING:
        await channel.send("⚠️ 議論が完了すると自動でサマリーが出力されます。完了までお待ちください。")
        return
    # No.9: IDLE での /summary → 正常（再表示）
    if not last_discussion["summary"]:
        await channel.send("⚠️ サマリーがまだ生成されていません。")
        return
    await generate_and_post_summary(channel)


async def handle_continue(channel, parsed: dict):
    global bot_state, last_discussion
    # No.4: INITIAL での /continue
    if bot_state == BotState.INITIAL:
        await channel.send(
            "⚠️ 議論履歴がありません。まず /discuss /AI=(Claude,Gemini) テーマ で議論を開始してください。"
        )
        return
    # No.5: DISCUSSING での /continue
    if bot_state == BotState.DISCUSSING:
        await channel.send("⚠️ 議論は現在進行中です。終了するまでお待ちください。")
        return
    # No.6: IDLE → 正常継続
    turns = parsed["turns"] if parsed["turns"] is not None else DEFAULT_TURNS
    bot_state = BotState.DISCUSSING
    await channel.send(
        f"▶️ 議論を継続します（{turns}ターン）\n"
        f"テーマ：{last_discussion['theme']}"
    )
    asyncio.create_task(
        run_discussion_loop(
            channel,
            last_discussion["ai_list"],
            turns,
            last_discussion["theme"],
            last_discussion["history"].copy(),
        )
    )


async def handle_discuss(channel, parsed: dict):
    global bot_state, last_discussion

    # No.10: DISCUSSING 中の /discuss
    if bot_state == BotState.DISCUSSING:
        await channel.send(
            "⚠️ 現在議論が進行中です。/stop で終了してから新しい議論を開始してください。"
        )
        return

    # /AI= 未指定ならデフォルトでエラー（discussには2種類以上必須）
    ai_list = parsed["ai_list"] if parsed["ai_list"] is not None else []

    # No.11: AI 1種類以下
    if len(ai_list) < 2:
        await channel.send(
            "⚠️ /discuss は2種類以上のAIを指定してください。例：/discuss /AI=(Claude,Gemini) テーマ"
        )
        return

    # No.12: テーマなし
    if not parsed["theme"]:
        await channel.send(
            "⚠️ テーマを入力してください。例：@AI-DiscussBot /discuss /AI=(Claude,Gemini) 5Gの未来"
        )
        return

    turns = parsed["turns"] if parsed["turns"] is not None else DEFAULT_TURNS

    last_discussion = {
        "ai_list": ai_list,
        "turns":   turns,
        "theme":   parsed["theme"],
        "history": [],
        "summary": "",
    }
    bot_state = BotState.DISCUSSING

    ai_names = "、".join(AI_DISPLAY.get(a, a) for a in ai_list)
    await channel.send(
        f"🗣️ 議論を開始します！\n"
        f"テーマ：{parsed['theme']}\n"
        f"参加AI：{ai_names}　／　ターン数：{turns}"
    )

    asyncio.create_task(
        run_discussion_loop(channel, ai_list, turns, parsed["theme"], [])
    )


async def handle_single_ai(channel, ai_list: list, question: str):
    """モード1（単体）または モード2（複数独立）"""
    if not question:
        # No.13: 質問内容が空
        await channel.send("⚠️ 質問内容が空です。テーマを入力してください。")
        return

    for ai in ai_list:
        await channel.send(f"{AI_EMOJI.get(ai, '🤖')} **{AI_DISPLAY.get(ai, ai)}** が回答中…")
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, call_ai, ai, question)
        header = f"{AI_EMOJI.get(ai, '🤖')} **{AI_DISPLAY.get(ai, ai)}**："
        await send_long(channel, header + "\n" + response)


# ─────────────────────────────────────────
#  on_message メインルーティング
# ─────────────────────────────────────────
@client.event
async def on_ready():
    print(f"[INFO] Logged in as {client.user}")
    print(f"[INFO] State: {bot_state}")


@client.event
async def on_message(message: discord.Message):
    global bot_state

    # 自分自身のメッセージは無視
    if message.author == client.user:
        return

    # メンションされていない場合は無視
    if client.user not in message.mentions:
        return

    # メンション部分を除去
    content = re.sub(r"<@!?[0-9]+>", "", message.content).strip()
    print(f"[DEBUG] on_message: {content!r}  state={bot_state}")

    channel = message.channel

    # ─── パース ───
    parsed = parse_message(content)

    # パースエラー（/AI= や /turns= の不正値）
    if parsed["error_msg"]:
        await channel.send(parsed["error_msg"])
        return

    # /help
    if parsed["has_help"]:
        await handle_help(channel)
        return

    # 未知コマンドが含まれている場合
    if parsed["unknown_commands"]:
        for unk in parsed["unknown_commands"]:
            await channel.send(
                f"⚠️ 不明なコマンド {unk} です。/help でコマンド一覧を確認してください。"
            )
        return

    # /stop
    if parsed["has_stop"]:
        await handle_stop(channel)
        return

    # /summary
    if parsed["has_summary"]:
        await handle_summary(channel)
        return

    # /continue
    if parsed["has_continue"]:
        await handle_continue(channel, parsed)
        return

    # /discuss
    if parsed["has_discuss"]:
        await handle_discuss(channel, parsed)
        return

    # ─── 通常質問（モード1 / モード2） ───
    # DISCUSSING 中でも状態変化なしで回答（No.14）
    ai_list = parsed["ai_list"] if parsed["ai_list"] is not None else ["claude"]
    await handle_single_ai(channel, ai_list, parsed["theme"])


# ─────────────────────────────────────────
#  起動
# ─────────────────────────────────────────
if __name__ == "__main__":
    client.run(TOKEN)
