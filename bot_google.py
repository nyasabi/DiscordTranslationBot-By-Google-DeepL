"""
国旗リアクション翻訳 Discord BOT (Google 翻訳 + DeepL)

メッセージに国旗の絵文字リアクションが付けられると、
その国旗に対応する言語へメッセージを翻訳し、
チャンネルに返信（全員に見える形）で投稿します。
"""

import asyncio
import html
import json
import logging
import math
import os
import re
import time
from collections import OrderedDict

import aiohttp
import discord
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

# 無料プランのキーは末尾が ":fx"（無料: api-free.deepl.com / 有料: api.deepl.com）
DEEPL_ENDPOINT = (
    "https://api-free.deepl.com/v2/translate"
    if DEEPL_API_KEY.endswith(":fx")
    else "https://api.deepl.com/v2/translate"
)

# DeepL の応答を待つ最大秒数
DEEPL_TIMEOUT = 15

GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
GOOGLE_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"

# Google 翻訳の応答を待つ最大秒数（超えたら DeepL に切り替える）
GOOGLE_TIMEOUT = 15

# 起動ログに表示。新しいコードが動いているかの確認用
BOT_VERSION = "10-google"

# 翻訳対象の最大文字数（長すぎるメッセージは拒否）
MAX_SOURCE_CHARS = 4000

DISCORD_MESSAGE_LIMIT = 2000
CACHE_SIZE = 500

# 国旗絵文字 -> (表示名, DeepL の target_lang コード)
FLAG_LANGUAGES: dict[str, tuple[str, str]] = {
    "🇯🇵": ("日本語", "JA"),
    "🇺🇸": ("English", "EN-US"),
    "🇨🇦": ("English", "EN-US"),
    "🇬🇧": ("English", "EN-GB"),
    "🇦🇺": ("English", "EN-GB"),
    "🇨🇳": ("简体中文", "ZH-HANS"),
    "🇹🇼": ("繁體中文", "ZH-HANT"),
    "🇭🇰": ("繁體中文(香港)", "ZH-HANT"),
    "🇰🇷": ("한국어", "KO"),
    "🇫🇷": ("Français", "FR"),
    "🇩🇪": ("Deutsch", "DE"),
    "🇪🇸": ("Español", "ES"),
    "🇲🇽": ("Español (Latinoamérica)", "ES-419"),
    "🇮🇹": ("Italiano", "IT"),
    "🇵🇹": ("Português", "PT-PT"),
    "🇧🇷": ("Português (Brasil)", "PT-BR"),
    "🇷🇺": ("Русский", "RU"),
    "🇺🇦": ("Українська", "UK"),
    "🇵🇱": ("Polski", "PL"),
    "🇳🇱": ("Nederlands", "NL"),
    "🇸🇪": ("Svenska", "SV"),
    "🇳🇴": ("Norsk", "NB"),
    "🇩🇰": ("Dansk", "DA"),
    "🇫🇮": ("Suomi", "FI"),
    "🇬🇷": ("Ελληνικά", "EL"),
    "🇹🇷": ("Türkçe", "TR"),
    "🇸🇦": ("العربية", "AR"),
    "🇦🇪": ("العربية", "AR"),
    "🇮🇱": ("עברית", "HE"),
    "🇨🇿": ("Čeština", "CS"),
    "🇭🇺": ("Magyar", "HU"),
    "🇷🇴": ("Română", "RO"),
    "🇧🇬": ("Български", "BG"),
    "🇪🇪": ("Eesti", "ET"),
    "🇱🇹": ("Lietuvių", "LT"),
    "🇱🇻": ("Latviešu", "LV"),
    "🇸🇰": ("Slovenčina", "SK"),
    "🇸🇮": ("Slovenščina", "SL"),
    "🇮🇩": ("Bahasa Indonesia", "ID"),
    # 以下は DeepL が対応していない場合があります（その場合は「未対応」と通知します）
    "🇹🇭": ("ภาษาไทย", "TH"),
    "🇻🇳": ("Tiếng Việt", "VI"),
    "🇮🇳": ("हिन्दी", "HI"),
    "🇧🇩": ("বাংলা", "BN"),
    "🇲🇾": ("Bahasa Melayu", "MS"),
    "🇵🇭": ("Filipino", "TL"),
}

# 標準の言語リストにない可能性があり、enable_beta_languages を付けて試す言語
BETA_CODES = {"TH", "VI", "HI", "BN", "MS", "TL"}

# DeepL のコード -> Google 翻訳（Cloud Translation）の言語コード
GOOGLE_CODES = {
    "JA": "ja",
    "EN-US": "en",
    "EN-GB": "en",
    "ZH-HANS": "zh-CN",
    "ZH-HANT": "zh-TW",
    "KO": "ko",
    "FR": "fr",
    "DE": "de",
    "ES": "es",
    "ES-419": "es",
    "IT": "it",
    "PT-PT": "pt",
    "PT-BR": "pt",
    "RU": "ru",
    "UK": "uk",
    "PL": "pl",
    "NL": "nl",
    "SV": "sv",
    "NB": "no",
    "DA": "da",
    "FI": "fi",
    "EL": "el",
    "TR": "tr",
    "AR": "ar",
    "HE": "iw",
    "CS": "cs",
    "HU": "hu",
    "RO": "ro",
    "BG": "bg",
    "ET": "et",
    "LT": "lt",
    "LV": "lv",
    "SK": "sk",
    "SL": "sl",
    "ID": "id",
    "TH": "th",
    "VI": "vi",
    "HI": "hi",
    "BN": "bn",
    "MS": "ms",
    "TL": "tl",
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("translate-bot")

intents = discord.Intents.default()
intents.reactions = True
intents.guilds = True
intents.messages = True
# メッセージ本文を読むために必要（Developer Portal で Message Content Intent を ON にすること）
intents.message_content = True
# !t-whitelist add all でサーバーの全メンバーを取得するために必要（Developer Portal で Server Members Intent を ON にすること）
intents.members = True

# 起動時に全サーバーのメンバーを取得しない（起動が遅くなり、メモリも使うため）。
# メンバー一覧は !t-whitelist add all を実行したときに、そのサーバーの分だけ取得する
client = discord.Client(intents=intents, chunk_guilds_at_startup=False)

http_session: aiohttp.ClientSession | None = None

# 同じメッセージ x 同じ言語の重複翻訳を防ぐためのキャッシュ / 実行中セット
translation_cache: "OrderedDict[tuple[int, str], bool]" = OrderedDict()
in_flight: set[tuple[int, str]] = set()


# 翻訳機能の利用を禁止されたユーザー（サーバーごとに管理。再起動しても残るよう JSON に保存）
BANNED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banned_users.json")


def load_banned() -> dict[int, set[int]]:
    try:
        with open(BANNED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # {"サーバーID": [ユーザーID, ...]} の形式のみ読み込む
        if isinstance(data, dict):
            return {int(g): {int(u) for u in users} for g, users in data.items()}
        return {}
    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("banned_users.json の読み込みに失敗しました")
        return {}


def save_banned() -> None:
    tmp = BANNED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(g): sorted(u) for g, u in banned_users.items() if u}, f)
    os.replace(tmp, BANNED_FILE)


banned_users: dict[int, set[int]] = load_banned()


# 例外チャンネル（サーバーごと）。ここでは管理者権限を持つ人だけが翻訳できる
EXCEPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "except_channels.json")


def load_except() -> dict[int, set[int]]:
    try:
        with open(EXCEPT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {int(g): {int(c) for c in chs} for g, chs in data.items()}
        return {}
    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("except_channels.json の読み込みに失敗しました")
        return {}


def save_except() -> None:
    tmp = EXCEPT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(g): sorted(c) for g, c in except_channels.items() if c}, f)
    os.replace(tmp, EXCEPT_FILE)


except_channels: dict[int, set[int]] = load_except()


# ホワイトリスト（サーバーごと）。ON のときは許可リストのユーザー（と管理者）だけが翻訳できる
# bot_google.py と bot_owner_google.py で同じファイルを共有する。もう一方の BOT が書き換えたら、更新時刻の変化を見て読み直す
WHITELIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whitelist.json")

whitelist_enabled: set[int] = set()
whitelist_users: dict[int, set[int]] = {}
# 最後に読み込んだ／保存したときの whitelist.json の更新時刻
_whitelist_mtime: int | None = None
# 読み込みに失敗したときの更新時刻（同じ壊れたファイルでエラーログを出し続けないため）
_whitelist_bad_mtime: int | None = None


def _whitelist_mtime_now() -> int | None:
    try:
        return os.stat(WHITELIST_FILE).st_mtime_ns
    except FileNotFoundError:
        return None


def _parse_whitelist(data) -> tuple[set[int], dict[int, set[int]]]:
    """{"サーバーID": {"enabled": bool, "users": [ユーザーID, ...]}} を読む。形式が不正なら例外を投げる。"""
    if not isinstance(data, dict):
        raise ValueError("whitelist.json の形式が不正です")
    enabled: set[int] = set()
    users: dict[int, set[int]] = {}
    for g, v in data.items():
        if not isinstance(v, dict) or not isinstance(v.get("users", []), list):
            raise ValueError(f"whitelist.json のサーバー {g} の形式が不正です")
        if v.get("enabled"):
            enabled.add(int(g))
        users[int(g)] = {int(u) for u in v.get("users", [])}
    return enabled, users


def refresh_whitelist(strict: bool = False) -> None:
    """whitelist.json が前回から変わっていれば読み直す。

    読み込みに失敗した場合、strict なら例外を投げ、そうでなければ前回の内容を使い続ける
    （ファイルが壊れたときに、許可リストが黙って外れて誰でも翻訳できる状態にならないようにするため）。
    ファイルが消えた場合も前回の内容を使い続ける。
    """
    global whitelist_enabled, whitelist_users, _whitelist_mtime, _whitelist_bad_mtime
    mtime = _whitelist_mtime_now()
    if mtime is None or mtime in (_whitelist_mtime, _whitelist_bad_mtime):
        return
    try:
        with open(WHITELIST_FILE, encoding="utf-8") as f:
            enabled, users = _parse_whitelist(json.load(f))
    except Exception:
        if strict:
            raise
        _whitelist_bad_mtime = mtime
        log.exception("whitelist.json の読み込みに失敗したため、前回の内容を使い続けます")
        return
    whitelist_enabled, whitelist_users, _whitelist_mtime = enabled, users, mtime


def save_whitelist(enabled: set[int], users: dict[int, set[int]]) -> None:
    global _whitelist_mtime
    # 2つの BOT が同時に保存しても一時ファイルがぶつからないよう、プロセスごとに名前を分ける
    tmp = f"{WHITELIST_FILE}.{os.getpid()}.tmp"
    guilds = enabled | {g for g, u in users.items() if u}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                str(g): {"enabled": g in enabled, "users": sorted(users.get(g, ()))}
                for g in guilds
            },
            f,
        )
    os.replace(tmp, WHITELIST_FILE)
    _whitelist_mtime = _whitelist_mtime_now()


def update_whitelist(guild_id: int, change) -> str:
    """最新の whitelist.json を読み直してから change を適用して保存し、返信文を返す。

    change(enabled, users) は (新しい enabled, 新しい users, 返信文) を返す。
    保存に失敗したときはメモリ上の内容も変えずに例外を投げる。
    """
    global whitelist_enabled, whitelist_users
    refresh_whitelist()
    enabled = guild_id in whitelist_enabled
    users = set(whitelist_users.get(guild_id, ()))
    new_enabled, new_users, text = change(enabled, users)
    if new_enabled == enabled and new_users == users:
        return text
    all_enabled = whitelist_enabled | {guild_id} if new_enabled else whitelist_enabled - {guild_id}
    all_users = {**whitelist_users, guild_id: new_users}
    save_whitelist(all_enabled, all_users)
    whitelist_enabled, whitelist_users = all_enabled, all_users
    return text


try:
    refresh_whitelist(strict=True)
except Exception:
    log.critical("whitelist.json を読み込めません。ファイルを修正するか削除してから起動してください。")
    raise


def whitelist_allows(guild_id: int, user_id: int) -> bool:
    """ホワイトリストが OFF、または許可リストに入っていれば True（管理者の判定は呼び出し側で行う）。"""
    refresh_whitelist()
    if guild_id not in whitelist_enabled:
        return True
    return user_id in whitelist_users.get(guild_id, ())


class SameLanguageError(Exception):
    """原文がすでに目的の言語だった。"""


class QuotaExceededError(Exception):
    """DeepL の文字数上限に達した。"""


class UnsupportedLanguageError(Exception):
    """DeepL がその言語に対応していない。"""


# Discord のメンション・カスタム絵文字・タイムスタンプ・URL は翻訳せず、そのまま残す
_PROTECT = re.compile(
    r"<a?:\w+:\d+>|<@[!&]?\d+>|<#\d+>|<t:\d+(?::[A-Za-z])?>|https?://[^\s<>]+"
)


def _to_xml(text: str) -> str:
    """保護する部分を <x>…</x> で囲み、残りを XML エスケープする。"""
    parts: list[str] = []
    last = 0
    for m in _PROTECT.finditer(text):
        parts.append(html.escape(text[last : m.start()], quote=False))
        parts.append("<x>" + html.escape(m.group(), quote=False) + "</x>")
        last = m.end()
    parts.append(html.escape(text[last:], quote=False))
    return "".join(parts)


def _from_xml(text: str) -> str:
    return html.unescape(re.sub(r"</?x>", "", text))


def _normalize(s: str) -> str:
    return "".join(s.split()).lower()


async def deepl_translate(text: str, target_code: str) -> str:
    """DeepL API で text を target_code（例: "JA", "EN-US"）に翻訳して返す。"""
    assert http_session is not None

    payload = {
        "text": [_to_xml(text)],
        "target_lang": target_code,
        "tag_handling": "xml",
        "ignore_tags": ["x"],
    }
    if target_code in BETA_CODES:
        payload["enable_beta_languages"] = True
    headers = {
        "Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}",
        "Content-Type": "application/json",
    }

    status, body = 0, ""
    for attempt in range(3):
        async with http_session.post(
            DEEPL_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DEEPL_TIMEOUT),
        ) as resp:
            status, body = resp.status, await resp.text()
        # 混雑時は少し待って再試行
        if status in (429, 503) and attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        break

    if status == 456:
        raise QuotaExceededError()
    if status == 400 and "target_lang" in body:
        raise UnsupportedLanguageError()
    if status != 200:
        raise RuntimeError(f"DeepL API error {status}: {body[:300]}")

    t = json.loads(body)["translations"][0]
    out = _from_xml(t["text"]).strip()
    if not out:
        raise RuntimeError("DeepL returned an empty translation")

    # 原文がすでに目的の言語だった場合
    detected = (t.get("detected_source_language") or "").upper()
    base = target_code.split("-")[0]
    if detected == base and ("-" not in target_code or _normalize(out) == _normalize(text)):
        raise SameLanguageError()
    return out


def _esc_html(s: str) -> str:
    return html.escape(s, quote=False).replace("\n", "<br>")


def _to_google_html(text: str) -> str:
    """保護する部分を notranslate の span で囲み、残りを HTML エスケープして改行を <br> にする。"""
    parts: list[str] = []
    last = 0
    for m in _PROTECT.finditer(text):
        parts.append(_esc_html(text[last : m.start()]))
        parts.append(
            '<span class="notranslate">' + html.escape(m.group(), quote=False) + "</span>"
        )
        last = m.end()
    parts.append(_esc_html(text[last:]))
    return "".join(parts)


def _from_google_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?span[^>]*>", "", text, flags=re.I)
    text = html.unescape(text)
    return re.sub(r"[ \t]*\n[ \t]*", "\n", text)


async def google_translate(text: str, target_code: str) -> str:
    """Google 翻訳（Cloud Translation API v2）で text を翻訳して返す。

    target_code は DeepL 形式のコード（例: "JA", "EN-US"）。
    """
    assert http_session is not None

    payload = {
        "q": [_to_google_html(text)],
        "target": GOOGLE_CODES[target_code],
        "format": "html",
    }
    headers = {"X-goog-api-key": GOOGLE_API_KEY, "Content-Type": "application/json"}

    status, body = 0, ""
    for attempt in range(3):
        async with http_session.post(
            GOOGLE_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=GOOGLE_TIMEOUT),
        ) as resp:
            status, body = resp.status, await resp.text()
        # 混雑時は少し待って再試行
        if status in (429, 503) and attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        break
    if status != 200:
        raise RuntimeError(f"Google API error {status}: {body[:300]}")

    t = json.loads(body)["data"]["translations"][0]
    out = _from_google_html(t["translatedText"]).strip()
    if not out:
        raise RuntimeError("Google returned an empty translation")

    # 原文がすでに目的の言語だった場合
    google_code = GOOGLE_CODES[target_code].lower()
    detected = (t.get("detectedSourceLanguage") or "").lower()
    if detected and (
        detected == google_code
        or (
            detected.split("-")[0] == google_code.split("-")[0]
            and _normalize(out) == _normalize(text)
        )
    ):
        raise SameLanguageError()
    return out


async def translate_auto(text: str, code: str) -> tuple[str, str]:
    """(翻訳結果, 使ったエンジン名) を返す。

    まず Google 翻訳を使い、応答しない・エラーになった場合は DeepL に切り替える。
    原文がすでに目的の言語だった場合（SameLanguageError）は切り替えずに投げ直す。
    """
    try:
        return await google_translate(text, code), "Google"
    except SameLanguageError:
        raise
    except Exception as e:
        log.warning("[%s] Google 翻訳が使えないため DeepL に切り替えます: %r", code, e)
    return await deepl_translate(text, code), "DeepL"


def build_source_text(message: discord.Message) -> str:
    """翻訳対象のテキストを取り出す（本文が空なら embed の説明文を使う）。"""
    text = message.content.strip()
    if not text and message.embeds:
        parts = []
        for e in message.embeds:
            if e.title:
                parts.append(e.title)
            if e.description:
                parts.append(e.description)
        text = "\n".join(parts).strip()
    return text


def split_for_discord(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Discord の文字数制限に収まるよう分割する。"""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


@client.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", client.user, client.user.id)
    log.info("bot version: %s", BOT_VERSION)


_ID_PATTERN = re.compile(r"\d{17,20}")

HELP_TEXT = (
    "📖 **翻訳BOT ヘルプ**\n"
    "メッセージに国旗の絵文字でリアクションすると、その言語に翻訳して返信します。\n"
    "（例: 🇺🇸 英語、🇰🇷 韓国語、🇫🇷 フランス語）\n"
    "翻訳は通常 Google 翻訳で行い、応答しないときは DeepL に切り替えます。使ったほうは結果の先頭に表示されます。\n\n"
    "**コマンド**\n"
    "`!help` : このヘルプを表示します。\n"
    "`!t-t 🇺🇸 文章` : 国旗の絵文字で言語を指定して、Google 翻訳と DeepL の両方で翻訳し、結果を並べて表示します。\n\n"
    "**管理者のみ**\n"
    "`!ping` : 遅延（ms）を表示します。\n"
    "`!t-ban @ユーザー` : そのユーザーの翻訳機能を禁止します。\n"
    "`!t-unban @ユーザー` : 禁止を解除します。\n"
    "`!t-except add [#チャンネル]` : 例外チャンネルに追加します（省略すると今のチャンネル）。\n"
    "`!t-except remove [#チャンネル]` : 例外チャンネルから外します。\n"
    "`!t-except list` : 例外チャンネルの一覧を表示します。\n"
    "`!t-whitelist on` / `off` : ホワイトリストモードを切り替えます。\n"
    "`!t-whitelist add all` : 今サーバーにいる全員を許可リストに追加します（後から入った人は含まれません）。\n"
    "`!t-whitelist add @ユーザー` : そのユーザーを許可リストに追加します。\n"
    "`!t-whitelist remove @ユーザー` : 許可リストから外します。\n"
    "`!t-whitelist list` : 状態と許可リストの人数を表示します。\n\n"
    "例外チャンネルでは、管理者権限を持つ人だけが翻訳できます。\n"
    "ホワイトリストモードが ON のときは、許可リストに入っている人（と管理者）だけが翻訳できます。"
)




def is_admin_member(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def handle_t_t(message: discord.Message, arg: str) -> None:
    """!t-t 国旗 文章 : Google 翻訳と DeepL の両方で翻訳し、結果を並べて表示する。"""
    no_mentions = discord.AllowedMentions.none()
    usage = "使い方: `!t-t 🇺🇸 翻訳したい文章`（国旗の絵文字のあとに文章を書いてください）"

    # 禁止ユーザー・例外チャンネルでは、リアクション翻訳と同じく何もしない
    if message.author.id in banned_users.get(message.guild.id, ()):
        return
    # ホワイトリストモードでは、許可リストにない人（管理者を除く）は何もしない
    if not whitelist_allows(message.guild.id, message.author.id) and not is_admin_member(
        message.author
    ):
        return
    restricted = except_channels.get(message.guild.id, set())
    parent_id = getattr(message.channel, "parent_id", None)
    if (message.channel.id in restricted or (parent_id and parent_id in restricted)) and not is_admin_member(
        message.author
    ):
        return

    arg = arg.strip()
    flag = arg[:2]
    text = arg[2:].strip()
    if flag not in FLAG_LANGUAGES or not text:
        await message.reply(usage, mention_author=False, allowed_mentions=no_mentions)
        return

    if len(text) > MAX_SOURCE_CHARS:
        await message.reply(
            f"メッセージが長すぎるため翻訳できません（{MAX_SOURCE_CHARS}文字まで）。",
            mention_author=False,
            allowed_mentions=no_mentions,
        )
        return

    display_name, code = FLAG_LANGUAGES[flag]
    async with message.channel.typing():
        google_res, deepl_res = await asyncio.gather(
            google_translate(text, code),
            deepl_translate(text, code),
            return_exceptions=True,
        )

    def render(engine: str, res) -> str:
        if isinstance(res, SameLanguageError):
            return "すでにその言語の文章です。"
        if isinstance(res, BaseException):
            log.warning("[%s] %s での翻訳に失敗しました: %r", code, engine, res)
            return "翻訳に失敗しました。"
        return res

    body = (
        f"{flag} **{display_name}** に翻訳しました（Google ＆ DeepL）\n\n"
        f"**Google**\n{render('Google', google_res)}\n\n"
        f"**DeepL**\n{render('DeepL', deepl_res)}"
    )
    chunks = split_for_discord(body)
    await message.reply(chunks[0], mention_author=False, allowed_mentions=no_mentions)
    for chunk in chunks[1:]:
        await message.channel.send(chunk, allowed_mentions=no_mentions)


@client.event
async def on_message(message: discord.Message):
    """管理コマンド: !ping / !t-ban / !t-unban（サーバーの管理者権限を持つ人のみ）。"""
    if message.author.bot or message.guild is None:
        return

    cmd, _, arg = message.content.strip().partition(" ")
    cmd = cmd.lower()
    if cmd not in ("!help", "!t-t", "!ping", "!t-ban", "!t-unban", "!t-except", "!t-whitelist"):
        return

    # !help は誰でも使える
    if cmd == "!help":
        await message.reply(
            HELP_TEXT, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
        )
        return

    # !t-t は誰でも使える（禁止ユーザー・例外チャンネルの制限は handle_t_t の中で確認）
    if cmd == "!t-t":
        await handle_t_t(message, arg)
        return

    # 管理者権限を持たない人には何も反応しない
    perms = getattr(message.author, "guild_permissions", None)
    if perms is None or not perms.administrator:
        return

    if cmd == "!ping":
        # Gateway(WebSocket) のレイテンシ
        latency = client.latency
        gateway_ms = f"{latency * 1000:.0f} ms" if not math.isnan(latency) else "N/A"

        # メッセージ送信にかかった時間（REST API の往復）
        start = time.perf_counter()
        sent = await message.reply("🏓 計測中...", mention_author=False)
        api_ms = (time.perf_counter() - start) * 1000

        await sent.edit(
            content=f"🏓 Pong!\nGateway: **{gateway_ms}**\nAPI: **{api_ms:.0f} ms**"
        )
        return

    # --- !t-except add / remove / list ---
    if cmd == "!t-except":
        no_mentions = discord.AllowedMentions.none()
        sub, _, rest = arg.strip().partition(" ")
        sub = sub.lower()
        channels = except_channels.setdefault(message.guild.id, set())

        if sub == "list":
            if channels:
                text = "例外チャンネル（管理者のみ翻訳できます）:\n" + "\n".join(
                    f"<#{c}>" for c in sorted(channels)
                )
            else:
                text = "例外チャンネルはありません。"
        elif sub in ("add", "remove"):
            m = _ID_PATTERN.search(rest)
            channel_id = int(m.group()) if m else message.channel.id  # 省略時は今のチャンネル
            if message.guild.get_channel_or_thread(channel_id) is None:
                text = "このサーバーのチャンネルが見つかりません。"
            elif sub == "add":
                if channel_id in channels:
                    text = f"<#{channel_id}> はすでに例外チャンネルです。"
                else:
                    channels.add(channel_id)
                    save_except()
                    text = f"🔒 <#{channel_id}> を例外チャンネルにしました。管理者だけが翻訳できます。"
            else:
                if channel_id in channels:
                    channels.discard(channel_id)
                    save_except()
                    text = f"🔓 <#{channel_id}> を例外チャンネルから外しました。"
                else:
                    text = f"<#{channel_id}> は例外チャンネルではありません。"
        else:
            text = "使い方: `!t-except add [#チャンネル]` / `!t-except remove [#チャンネル]` / `!t-except list`"

        await message.reply(text, mention_author=False, allowed_mentions=no_mentions)
        return

    # --- !t-whitelist on / off / add all / add <ID> / remove <ID> / list ---
    if cmd == "!t-whitelist":
        no_mentions = discord.AllowedMentions.none()
        guild = message.guild
        sub, _, rest = arg.strip().partition(" ")
        sub = sub.lower()
        rest = rest.strip()
        # 変更内容は change にまとめ、最後に update_whitelist で適用する
        # （await を挟まずに「読み直し → 変更 → 保存」するので、もう一方の BOT の変更を消さない）
        change = None
        text = ""

        if sub == "on":
            def change(enabled, users):
                return True, users, (
                    f"🔒 ホワイトリストモードを ON にしました（許可リスト: {len(users)}人）。"
                    "許可された人と管理者だけが翻訳できます。"
                )
        elif sub in ("off", "of"):
            def change(enabled, users):
                return False, users, "🔓 ホワイトリストモードを OFF にしました。"
        elif sub == "list":
            refresh_whitelist()
            state = "ON" if guild.id in whitelist_enabled else "OFF"
            count = len(whitelist_users.get(guild.id, ()))
            text = f"ホワイトリストモード: **{state}** / 許可リスト: {count}人"
        elif sub == "add" and rest.lower() == "all":
            # 「今」サーバーにいるメンバー（BOT を除く）だけを追加する
            async with message.channel.typing():
                try:
                    members = guild.members if guild.chunked else await guild.chunk()
                except Exception:
                    log.exception("メンバー一覧の取得に失敗しました")
                    members = []
            ids = {m.id for m in members if not m.bot}
            if not ids:
                text = (
                    "メンバー一覧を取得できませんでした。"
                    "Developer Portal で Server Members Intent が ON になっているか確認してください。"
                )
            else:
                def change(enabled, users):
                    added = len(ids - users)
                    users = users | ids
                    return enabled, users, (
                        f"✅ 今サーバーにいる {len(ids)}人 を許可リストに追加しました"
                        f"（新規 {added}人 / 合計 {len(users)}人）。"
                    )
        elif sub in ("add", "remove"):
            m = _ID_PATTERN.search(rest)
            if not m:
                text = f"使い方: `!t-whitelist {sub} @ユーザー` または `!t-whitelist {sub} ユーザーID`"
            elif sub == "add":
                target_id = int(m.group())
                # ロールやチャンネルの ID、存在しない ID を登録しないよう、ユーザーとして存在するか確認する
                if client.get_user(target_id) is None:
                    try:
                        await client.fetch_user(target_id)
                    except discord.NotFound:
                        text = f"ID `{target_id}` のユーザーが見つかりません。"
                    except discord.HTTPException:
                        log.exception("ユーザーの確認に失敗しました")
                        text = "ユーザーの確認に失敗しました。しばらくしてからもう一度お試しください。"
                if not text:
                    def change(enabled, users):
                        if target_id in users:
                            return enabled, users, f"<@{target_id}> はすでに許可リストに入っています。"
                        return enabled, users | {target_id}, f"✅ <@{target_id}> を許可リストに追加しました。"
            else:
                target_id = int(m.group())

                def change(enabled, users):
                    if target_id not in users:
                        return enabled, users, f"<@{target_id}> は許可リストに入っていません。"
                    return enabled, users - {target_id}, f"🗑️ <@{target_id}> を許可リストから外しました。"
        else:
            text = (
                "使い方: `!t-whitelist on` / `!t-whitelist off` / `!t-whitelist add all` / "
                "`!t-whitelist add ユーザーID` / `!t-whitelist remove ユーザーID` / `!t-whitelist list`"
            )

        if change is not None:
            try:
                text = update_whitelist(guild.id, change)
            except Exception:
                log.exception("whitelist.json の保存に失敗しました")
                text = "⚠️ 設定を保存できなかったため、変更していません。しばらくしてからもう一度お試しください。"

        await message.reply(text, mention_author=False, allowed_mentions=no_mentions)
        return

    # --- !t-ban / !t-unban: メンションまたはユーザーIDを指定 ---
    no_mentions = discord.AllowedMentions.none()
    m = _ID_PATTERN.search(arg)
    if not m:
        await message.reply(
            f"使い方: `{cmd} @ユーザー` または `{cmd} ユーザーID`",
            mention_author=False,
            allowed_mentions=no_mentions,
        )
        return
    target_id = int(m.group())
    banned = banned_users.setdefault(message.guild.id, set())

    if cmd == "!t-ban":
        member = message.guild.get_member(target_id) or next(
            (u for u in message.mentions if u.id == target_id), None
        )
        is_admin = bool(
            member
            and getattr(member, "guild_permissions", None)
            and member.guild_permissions.administrator
        )
        if is_admin or (client.user and target_id == client.user.id):
            await message.reply(
                "このユーザーは禁止できません。",
                mention_author=False,
                allowed_mentions=no_mentions,
            )
            return
        if target_id in banned:
            text = f"<@{target_id}> はすでに翻訳機能を禁止されています。"
        else:
            banned.add(target_id)
            save_banned()
            text = f"🔨 <@{target_id}> を翻訳機能の利用禁止にしました。"
    else:  # !t-unban
        if target_id in banned:
            banned.discard(target_id)
            save_banned()
            text = f"✅ <@{target_id}> の翻訳機能の禁止を解除しました。"
        else:
            text = f"<@{target_id}> は禁止されていません。"

    await message.reply(text, mention_author=False, allowed_mentions=no_mentions)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # BOT 自身のリアクションは無視
    if client.user and payload.user_id == client.user.id:
        return

    # 翻訳機能を禁止されたユーザーのリアクションは無反応
    if payload.guild_id and payload.user_id in banned_users.get(payload.guild_id, ()):
        return

    flag = str(payload.emoji)
    if flag not in FLAG_LANGUAGES:
        return

    # ホワイトリストモードでは、許可リストにない人（管理者を除く）のリアクションは無反応
    if payload.guild_id and not whitelist_allows(payload.guild_id, payload.user_id):
        member = payload.member
        if member is None:
            guild = client.get_guild(payload.guild_id)
            try:
                member = guild.get_member(payload.user_id) or await guild.fetch_member(
                    payload.user_id
                )
            except Exception:
                member = None
        if not is_admin_member(member):
            return

    # 例外チャンネル（スレッドの場合は親チャンネルも対象）では、管理者権限を持つ人だけが翻訳できる
    if payload.guild_id:
        restricted = except_channels.get(payload.guild_id, set())
        if restricted:
            ch = client.get_channel(payload.channel_id)
            parent_id = getattr(ch, "parent_id", None)
            if payload.channel_id in restricted or (parent_id and parent_id in restricted):
                member = payload.member
                if member is None:
                    guild = client.get_guild(payload.guild_id)
                    try:
                        member = guild.get_member(payload.user_id) or await guild.fetch_member(
                            payload.user_id
                        )
                    except Exception:
                        member = None
                if member is None or not member.guild_permissions.administrator:
                    return

    display_name, language = FLAG_LANGUAGES[flag]
    key = (payload.message_id, language)

    # すでに翻訳済み / 翻訳中なら何もしない
    if key in translation_cache or key in in_flight:
        return
    in_flight.add(key)

    try:
        channel = client.get_channel(payload.channel_id) or await client.fetch_channel(
            payload.channel_id
        )
        message = await channel.fetch_message(payload.message_id)

        # BOT の翻訳結果自体は再翻訳しない
        if client.user and message.author.id == client.user.id:
            return

        source = build_source_text(message)
        if not source:
            return
        if len(source) > MAX_SOURCE_CHARS:
            await message.reply(
                f"{flag} メッセージが長すぎるため翻訳できません（{MAX_SOURCE_CHARS}文字まで）。",
                mention_author=False,
            )
            return

        async with channel.typing():
            translated, engine = await translate_auto(source, language)

        header = f"{flag} **{display_name}** に翻訳しました（{engine}）:\n"
        chunks = split_for_discord(header + translated)

        # 最初の1通は元メッセージへの返信、残りは続けて投稿（全員に表示される）
        first = await message.reply(
            chunks[0],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        for chunk in chunks[1:]:
            await channel.send(
                chunk, allowed_mentions=discord.AllowedMentions.none()
            )

        translation_cache[key] = True
        while len(translation_cache) > CACHE_SIZE:
            translation_cache.popitem(last=False)

    except SameLanguageError:
        try:
            await message.reply(
                f"{flag} すでに {display_name} の文章です。",
                mention_author=False,
                delete_after=15,
            )
        except Exception:
            pass
    except discord.Forbidden:
        log.warning("権限不足: channel=%s", payload.channel_id)
    except discord.NotFound:
        log.warning("メッセージまたはチャンネルが見つかりません")
    except Exception as e:
        log.exception("翻訳に失敗しました")
        try:
            channel = client.get_channel(payload.channel_id)
            if channel:
                await channel.send(
                    f"{flag} 翻訳に失敗しました。しばらくしてからもう一度お試しください。",
                    delete_after=15,
                )
        except Exception:
            pass
    finally:
        in_flight.discard(key)


async def main():
    global http_session
    # BOT の接続より先に作っておく（on_ready より前に届いたリアクションでも翻訳できるように）
    async with aiohttp.ClientSession() as session:
        http_session = session
        async with client:
            await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
