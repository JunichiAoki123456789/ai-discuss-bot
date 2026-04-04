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

# ─────────────────────────────────────────
# 対応AI定義（将来の GPT 等追加はここだけ変更）
# ─────────────────────────────────────────
SUPPORTED_AIS = {"Claude", "Gemini"}  # 表示名（先頭大文字）

AI_ICON = {
    "Claude": "🟧",
    "Gemini": "🟦",
}

# ─────────────────────────────────────────
# 状態管理（Step 7 で本格利用）
# ─────────────────────────────────────────
class BotState:
    INITIAL    = "INITIAL"
    DISCUSSING = "DISCUSSING"
    IDLE       = "IDLE"

bot_state = BotState.INITIAL

# 直前の議論セッション保存（/continue・/summary 用）
last_discussion = {
    "ai_list":   [],   # 例: ["Gemini", "Claude"]
    "turns":     3,
    "theme":     "",
    "history":   [],   # 会話履歴（案C：全履歴）
    "summary":   "",   # 直前サマリー文字列
}

# ─────────────────────────────────────────
# CLI 呼び出し
# ─────────────────────────────────────────
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


async def call_gemini(prompt: str, timeout: int = 60) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return f"⚠️ Gemini CLIがエラーを返しました。\n```\n{err[:500]}\n```"
        return stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return f"⏱️ タイムアウト：Geminiからの応答が{timeout}秒以内に届きませんでした。"
    except FileNotFoundError:
        return "⚠️ `gemini` コマンドが見つかりません。"
    except Exception as e:
        return f"⚠️ 予期しないエラーが発生しました：{e}"


async def call_ai(ai_name: str, prompt: str, timeout: int = 60) -> str:
    """AI名を受け取って対応する呼び出し関数にディスパッチ"""
    if ai_name == "Claude":
        return await call_claude(prompt, timeout)
    elif ai_name == "Gemini":
        return await call_gemini(prompt, timeout)
    else:
        return f"⚠️ 未実装のAIです：{ai_name}"


# ─────────────────────────────────────────
# Discord 長文送信
# ─────────────────────────────────────────
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
            suffix = (
                f"\n*(続き {i + 1}/{len(chunks)})*"
                if len(chunks) > 1 and i < len(chunks) - 1
                else ""
            )
            await channel.send(chunk + suffix)


# ─────────────────────────────────────────
# /AI= パーサー
# ─────────────────────────────────────────
def parse_ai_command(token: str, rest_tokens: list) -> tuple:
    """
    /AI=xxx または /AI=(xxx,yyy) を解析する。
    token     : /AI=... を含む1つ目のトークン
    rest_tokens: token の後に続くトークン列（カッコが閉じない場合に結合する）

    戻り値: (ai_list, consumed_count, error_message)
      - ai_list       : ["Claude", "Gemini"] のようなリスト（エラー時は空）
      - consumed_count: rest_tokens から追加消費したトークン数
      - error_message : エラーがある場合の文字列、なければ None
    """
    # /AI= の右辺を取り出す
    raw = token[len("/AI="):]  # /AI= の後ろ

    # --- ケース1: 値が空 ---
    if raw == "":
        # rest_tokens に ( があれば結合を試みる（スペース区切りで書いた場合）
        # 設計書の仕様では /AI= の直後に値が続くので、空ならエラー
        return [], 0, "⚠️ `/AI=` にAI名を指定してください。例：`/AI=Claude` または `/AI=(Claude,Gemini)`"

    # --- カッコあり ---
    if raw.startswith("("):
        # カッコが同じトークン内で閉じているか確認
        combined = raw
        consumed = 0
        # 閉じカッコが見つかるまで rest_tokens を結合
        while ")" not in combined and consumed < len(rest_tokens):
            combined += rest_tokens[consumed]
            consumed += 1

        if ")" not in combined:
            # 閉じカッコなし (No.17)
            return [], consumed, "⚠️ `/AI=` のカッコが閉じていません。例：`/AI=(Claude,Gemini)`"

        # カッコ内を抽出
        inner = combined[1:combined.index(")")]

        # カッコ内が空 (No.16)
        if inner.strip() == "":
            return [], consumed, "⚠️ `/AI=()` の中にAI名を指定してください。"

        # カンマで分割
        names_raw = [n.strip() for n in inner.split(",")]

        # 空要素チェック (No.19)
        if any(n == "" for n in names_raw):
            return [], consumed, "⚠️ `/AI=` の中に空の要素があります。例：`/AI=(Claude,Gemini)`"

        return _validate_ai_names(names_raw), consumed, _ai_names_error(names_raw)

    # --- カッコなし ---
    else:
        # カンマが含まれる = カッコなし複数指定 (No.18)
        if "," in raw:
            return [], 0, "⚠️ 複数AIを指定する場合はカッコが必要です。例：`/AI=(Claude,Gemini)`"

        # 単一AI名
        return _validate_ai_names([raw]), 0, _ai_names_error([raw])


def _normalize_ai_name(name: str) -> str:
    """claude → Claude のように先頭大文字に正規化"""
    return name.capitalize()


def _validate_ai_names(names_raw: list) -> list:
    """正規化した名前リストを返す（バリデーション前の変換のみ）"""
    return [_normalize_ai_name(n) for n in names_raw]


def _ai_names_error(names_raw: list) -> str | None:
    """AI名リストのバリデーションエラー文字列。問題なければ None"""
    normalized = [_normalize_ai_name(n) for n in names_raw]

    # 未対応AIチェック (No.20)
    unsupported = [n for n in normalized if n not in SUPPORTED_AIS]
    if unsupported:
        return f"⚠️ `{'、'.join(unsupported)}` は対応していないAIです。現在対応中のAI：Claude、Gemini"

    # 重複チェック (No.21)
    if len(normalized) != len(set(normalized)):
        return "⚠️ 同じAIが重複しています。各AIは1回のみ指定してください。"

    return None


# ─────────────────────────────────────────
# メッセージパーサー（v1.6 /AI= 対応版）
# ─────────────────────────────────────────
def parse_message(content: str) -> dict:
    """
    戻り値:
      {
        "ai_list":    ["Claude"] など、
        "commands":   ["/discuss", "/stop", ...],
        "turns":      3,
        "theme":      "質問テキスト",
        "errors":     ["エラーメッセージ", ...],
        "unknown_cmds": ["/xyz", ...],
      }
    """
    result = {
        "ai_list":      ["Claude"],  # デフォルト: Claudeのみ
        "commands":     [],
        "turns":        3,
        "theme":        "",
        "errors":       [],
        "unknown_cmds": [],
    }

    # @メンションを除去
    text = re.sub(r"<@!?\d+>", "", content).strip()
    tokens = text.split()

    theme_tokens = []
    ai_specified = False
    i = 0
    while i < len(tokens):
        i = i  # for clarity
        token = tokens[i]

        if not token.startswith("/"):
            theme_tokens.append(token)
            i += 1
            continue

        cmd_lower = token.lower()

        # /AI= 処理
        if cmd_lower.startswith("/ai="):
            rest = tokens[i + 1:]
            ai_list, consumed, err = parse_ai_command(token, rest)
            i += 1 + consumed
            if err:
                result["errors"].append(err)
                ai_specified = True  # エラーでも「指定あり」扱い（デフォルト適用しない）
            else:
                result["ai_list"] = ai_list
                ai_specified = True
            continue

        # /discuss
        if cmd_lower == "/discuss":
            result["commands"].append("/discuss")
            i += 1
            continue

        # /turns=N
        if cmd_lower.startswith("/turns="):
            val = token[len("/turns="):]
            if val == "":
                # No.24: 値が空
                result["errors"].append("⚠️ `/turns` に値を指定してください。例：`/turns=3`")
            else:
                try:
                    n = int(val)
                    if n <= 0:
                        # No.22: 0以下
                        result["errors"].append("⚠️ `/turns` は1以上の整数を指定してください。例：`/turns=3`")
                    else:
                        result["turns"] = n
                        result["commands"].append(token)
                except ValueError:
                    # No.23: 数字以外
                    result["errors"].append("⚠️ `/turns` の値が正しくありません。例：`/turns=3`")
            i += 1
            continue

        # /continue
        if cmd_lower == "/continue":
            result["commands"].append("/continue")
            i += 1
            continue

        # /stop
        if cmd_lower == "/stop":
            result["commands"].append("/stop")
            i += 1
            continue

        # /summary
        if cmd_lower == "/summary":
            result["commands"].append("/summary")
            i += 1
            continue

        # /help /?
        if cmd_lower in ("/help", "/?"):
            result["commands"].append("/help")
            i += 1
            continue

        # 未知のコマンド (No.25)
        result["unknown_cmds"].append(token)
        i += 1

    result["theme"] = " ".join(theme_tokens).strip()
    return result


# ─────────────────────────────────────────
# ヘルプメッセージ
# ─────────────────────────────────────────
def build_help_message() -> str:
    lines = [
        "**📖 AI-DiscussBot コマンド一覧**", "",
        "`@AI-DiscussBot [質問]`　　　　　　　→ Claudeのみ回答（デフォルト）",
        "`@AI-DiscussBot /AI=Claude [質問]`　→ Claudeのみ回答",
        "`@AI-DiscussBot /AI=Gemini [質問]`　→ Geminiのみ回答",
        "`@AI-DiscussBot /AI=(Claude,Gemini) [質問]`　→ 両者が独立回答", "",
        "**議論モード（Step 7以降で有効）：**",
        "`/discuss /AI=(Claude,Gemini) [テーマ]`　→ ターン制議論（デフォルト3ターン）",
        "`/discuss /AI=(Claude,Gemini) /turns=N [テーマ]`　→ ターン数指定",
        "`/continue`　→ 直前の議論を継続",
        "`/stop`　→ 議論を強制終了しサマリー出力",
        "`/summary`　→ 直前の議論サマリーを再表示", "",
        "`/help` または `/?`　→ このヘルプを表示", "",
        "**使用例：**",
        "`@AI-DiscussBot 量子コンピュータとは？`",
        "`@AI-DiscussBot /AI=(Claude,Gemini) AIの未来について教えて`",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────
# モード処理
# ─────────────────────────────────────────
async def handle_single_ai(channel: discord.TextChannel, ai_name: str, theme: str) -> None:
    """モード1: 単体AIへの質問"""
    if not theme:
        await channel.send(
            "⚠️ 質問内容が空です。テーマを入力してください。\n"
            "例：`@AI-DiscussBot 量子コンピュータとは？`"
        )
        return
    icon = AI_ICON.get(ai_name, "🤖")
    await channel.send(f"⏳ {ai_name}が考え中...")
    response = await call_ai(ai_name, theme)
    await send_long_message(channel, f"{icon} **{ai_name}：**\n{response}")


async def handle_multi_ai(channel: discord.TextChannel, ai_list: list, theme: str) -> None:
    """モード2: 複数AIへの独立回答"""
    if not theme:
        await channel.send(
            "⚠️ 質問内容が空です。テーマを入力してください。\n"
            "例：`@AI-DiscussBot /AI=(Claude,Gemini) 量子コンピュータとは？`"
        )
        return
    for ai_name in ai_list:
        icon = AI_ICON.get(ai_name, "🤖")
        await channel.send(f"⏳ {ai_name}が考え中...")
        response = await call_ai(ai_name, theme)
        await send_long_message(channel, f"{icon} **{ai_name}：**\n{response}")


# ─────────────────────────────────────────
# on_message メインロジック
# ─────────────────────────────────────────
@client.event
async def on_ready():
    print(f"✅ Bot起動成功: {client.user} (ID: {client.user.id})")


@client.event
async def on_message(message: discord.Message):
    print(f"📨 受信: '{message.content[:80]}' / author: {message.author}")

    if message.author == client.user:
        return

    # !ping（デバッグ用）
    if message.content == "!ping":
        await message.channel.send("🏓 Pong! Bot is alive.")
        return

    # メンションチェック
    if client.user not in message.mentions:
        print(f"  → メンションなし（client.user.id={client.user.id}）のため無視")
        return

    print(f"  → メンション確認OK、処理開始")

    # /help・/? の早期処理
    text_no_mention = re.sub(r"<@!?\d+>", "", message.content).strip()
    if "/help" in text_no_mention.lower() or "/?" in text_no_mention:
        await message.channel.send(build_help_message())
        return

    # メッセージ解析
    parsed = parse_message(message.content)
    print(f"  → parsed: ai_list={parsed['ai_list']}, commands={parsed['commands']}, "
          f"turns={parsed['turns']}, theme='{parsed['theme']}', "
          f"errors={parsed['errors']}, unknown_cmds={parsed['unknown_cmds']}")

    # ── エラーチェック ──────────────────────────
    # 未知コマンド (No.25)
    if parsed["unknown_cmds"]:
        for unk in parsed["unknown_cmds"]:
            await message.channel.send(
                f"⚠️ 不明なコマンド `{unk}` です。`/help` でコマンド一覧を確認してください。"
            )
        return

    # パースエラーがある場合は先に全部出力して終了
    if parsed["errors"]:
        for err in parsed["errors"]:
            await message.channel.send(err)
        return

    commands = parsed["commands"]
    ai_list  = parsed["ai_list"]
    theme    = parsed["theme"]

    # ── コマンド振り分け ──────────────────────

    # /discuss（Step 7で本格実装。現在はプレースホルダー）
    if "/discuss" in commands:
        # 状態チェック (No.10)
        if bot_state == BotState.DISCUSSING:
            await message.channel.send(
                "⚠️ 現在議論が進行中です。`/stop` で終了してから新しい議論を開始してください。"
            )
            return
        # AI数チェック (No.11)
        if len(ai_list) < 2:
            await message.channel.send(
                "⚠️ `/discuss` は2種類以上のAIを指定してください。"
                "例：`/discuss /AI=(Claude,Gemini) テーマ`"
            )
            return
        # テーマなしチェック (No.12)
        if not theme:
            await message.channel.send(
                "⚠️ テーマを入力してください。"
                "例：`@AI-DiscussBot /discuss /AI=(Claude,Gemini) 5Gの未来`"
            )
            return
        await message.channel.send("⚙️ 議論モード（`/discuss`）はStep 7で実装予定です。")
        return

    # /continue（Step 7で本格実装）
    if "/continue" in commands:
        if bot_state == BotState.INITIAL:
            await message.channel.send(
                "⚠️ 議論履歴がありません。まず `/discuss /AI=(Claude,Gemini) テーマ` で議論を開始してください。"
            )
        elif bot_state == BotState.DISCUSSING:
            await message.channel.send("⚠️ 議論は現在進行中です。終了するまでお待ちください。")
        else:
            await message.channel.send("⚙️ `/continue` はStep 7で実装予定です。")
        return

    # /stop（Step 7で本格実装）
    if "/stop" in commands:
        if bot_state != BotState.DISCUSSING:
            await message.channel.send("⚠️ 現在進行中の議論がありません。")
        else:
            await message.channel.send("⚙️ `/stop` はStep 7で実装予定です。")
        return

    # /summary（Step 7で本格実装）
    if "/summary" in commands:
        if bot_state == BotState.INITIAL:
            await message.channel.send(
                "⚠️ 議論履歴がありません。まず `/discuss` で議論を開始してください。"
            )
        elif bot_state == BotState.DISCUSSING:
            await message.channel.send(
                "⚠️ 議論が完了すると自動でサマリーが出力されます。完了までお待ちください。"
            )
        else:
            await message.channel.send("⚙️ `/summary` はStep 7で実装予定です。")
        return

    # ── AI回答モード ──────────────────────────
    # テーマなしチェック (No.13)
    if not theme:
        await message.channel.send("⚠️ 質問内容が空です。テーマを入力してください。")
        return

    if len(ai_list) == 1:
        # モード1: 単体AI
        await handle_single_ai(message.channel, ai_list[0], theme)
    else:
        # モード2: 複数AI独立回答
        await handle_multi_ai(message.channel, ai_list, theme)


client.run(TOKEN)
