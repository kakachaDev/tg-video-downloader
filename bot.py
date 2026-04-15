#!/usr/bin/env python3
"""
Telegram inline bot for downloading YouTube videos.

Flow:
  1. User types "@bot <youtube_url>" in any chat
  2. Bot shows quality options (audio, 240p, 480p, 720p, 1080p)
  3. User picks a quality → "Loading..." message appears via @bot
  4. Bot downloads the file, uploads to STORAGE_CHAT_ID, gets a file_id
  5. Bot calls editMessageMedia with inline_message_id to replace text with media
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputMediaAudio,
    InputMediaVideo,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    filters,
    InlineQueryHandler,
    MessageHandler,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
STORAGE_CHAT_ID: int = int(os.environ["STORAGE_CHAT_ID"])
ADMIN_USER_ID: int = int(os.environ["ADMIN_USER_ID"])

# Path where /cookie command saves the uploaded cookies.txt.
# yt-dlp will use it automatically on every download.
COOKIES_FILE: Path = Path(os.environ.get("COOKIES_FILE", "cookies.txt"))

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB — Telegram bot upload limit

QUALITY_OPTIONS = [
    {
        "key": "audio",
        "label": "🎵 Только аудио (MP3)",
        "format": "bestaudio/best",
        "is_audio": True,
    },
    {
        "key": "240p",
        "label": "📹 240p",
        "format": "bestvideo[height<=240][ext=mp4]+bestaudio/best[height<=240]/best",
        "is_audio": False,
    },
    {
        "key": "480p",
        "label": "📹 480p",
        "format": "bestvideo[height<=480][ext=mp4]+bestaudio/best[height<=480]/best",
        "is_audio": False,
    },
    {
        "key": "720p",
        "label": "📹 720p",
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720]/best",
        "is_audio": False,
    },
    {
        "key": "1080p",
        "label": "📹 1080p",
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio/best[height<=1080]/best",
        "is_audio": False,
    },
]

_quality_map: dict[str, dict] = {q["key"]: q for q in QUALITY_OPTIONS}

# In-memory cache: video_id -> {url, title, thumbnail}
# Survives the lifetime of the process; cleared on restart.
_video_cache: dict[str, dict] = {}

# Persistent file cache: "video_id|quality_key" -> file_id
# Saved to disk so it survives restarts.
_FILE_CACHE_PATH = Path("file_cache.json")
_file_cache: dict[str, str] = {}


def _load_file_cache() -> None:
    global _file_cache
    if _FILE_CACHE_PATH.exists():
        try:
            _file_cache = json.loads(_FILE_CACHE_PATH.read_text())
            logger.info("Loaded %d entries from file cache", len(_file_cache))
        except Exception as exc:
            logger.warning("Could not load file cache: %s", exc)
            _file_cache = {}


def _save_file_cache() -> None:
    try:
        _FILE_CACHE_PATH.write_text(json.dumps(_file_cache, indent=2))
    except Exception as exc:
        logger.warning("Could not save file cache: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _estimate_sizes(info: dict) -> dict[str, int | None]:
    """Return estimated file sizes in bytes per quality key. None = unknown."""
    formats = info.get("formats", [])
    sizes: dict[str, int | None] = {}

    def fsize(fmt: dict) -> int:
        return fmt.get("filesize") or fmt.get("filesize_approx") or 0

    audio_fmts = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    best_audio_size = max((fsize(f) for f in audio_fmts), default=0)

    # audio only
    sizes["audio"] = best_audio_size or None

    # video qualities
    for opt in QUALITY_OPTIONS:
        if opt["key"] == "audio":
            continue
        height = int(opt["key"].rstrip("p"))
        video_fmts = [
            f for f in formats
            if f.get("vcodec") != "none"
            and f.get("acodec") == "none"  # video-only stream
            and f.get("height") is not None
            and f.get("height") <= height
        ]
        if not video_fmts:
            # fallback: combined formats
            combined = [
                f for f in formats
                if f.get("height") is not None and f.get("height") <= height
            ]
            best = max(combined, key=lambda f: f.get("height") or 0, default=None)
            sizes[opt["key"]] = fsize(best) if best else None
        else:
            best_video = max(video_fmts, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
            total = fsize(best_video) + best_audio_size
            sizes[opt["key"]] = total if total > 0 else None

    return sizes


def _ytdlp_auth_opts() -> dict:
    """Return yt-dlp options for YouTube authentication, if cookies are available."""
    if COOKIES_FILE.exists():
        return {"cookiefile": str(COOKIES_FILE)}
    return {}


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log exceptions; ignore transient network/conflict errors silently."""
    exc = context.error
    if isinstance(exc, TelegramError) and "Conflict" in str(exc):
        # Two instances briefly overlapping (e.g. after os.execv restart) — not fatal
        logger.warning("Conflict error (likely brief overlap during restart): %s", exc)
        return
    logger.error("Unhandled exception", exc_info=exc)


async def _auto_update_ytdlp(context) -> None:
    """Job: update yt-dlp via pip, restart process if a new version was installed."""
    logger.info("Auto-updating yt-dlp…")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "-q"],
                capture_output=True,
                text=True,
            ),
        )
        if result.returncode == 0:
            logger.info("yt-dlp updated, restarting process…")
            Path("bot.pid").unlink(missing_ok=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            logger.warning("yt-dlp update failed: %s", result.stderr)
    except Exception as exc:
        logger.error("yt-dlp auto-update error: %s", exc)


def _extract_youtube_url(text: str) -> str | None:
    m = re.search(
        r"https?://(?:www\.)?(?:"
        r"youtube\.com/watch\?[^\s]*v=[\w\-_]+"
        r"|youtu\.be/[\w\-_]+"
        r"|youtube\.com/shorts/[\w\-_]+"
        r")",
        text,
    )
    return m.group(0) if m else None


def _get_video_id(url: str) -> str | None:
    for pattern in (
        r"[?&]v=([\w\-_]{11})",
        r"youtu\.be/([\w\-_]{11})",
        r"youtube\.com/shorts/([\w\-_]{11})",
    ):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


async def _fetch_video_info(url: str) -> dict | None:
    """Fetch video metadata without downloading (runs in thread pool)."""
    loop = asyncio.get_running_loop()

    def _run() -> dict | None:
        opts = {"quiet": True, "no_warnings": True, **_ytdlp_auth_opts()}
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                return ydl.extract_info(url, download=False)
            except Exception as exc:
                logger.error("fetch_video_info failed: %s", exc)
                return None

    return await loop.run_in_executor(None, _run)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username = context.bot.username
    await update.message.reply_html(
        f"👋 <b>Привет!</b>\n\n"
        f"Я скачиваю видео и аудио с YouTube прямо в чат.\n\n"
        f"<b>Как пользоваться:</b>\n"
        f"В любом чате напишите:\n"
        f"<code>@{username} https://youtu.be/...</code>\n\n"
        f"Выберите качество из предложенных вариантов — "
        f"и файл появится в чате."
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(f"Chat ID: <code>{chat.id}</code>", parse_mode=ParseMode.HTML)


async def cmd_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: update cookies.txt by uploading the file to the bot."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    doc = update.message.document
    if doc is None:
        # No file attached — show instructions
        current = f"Текущий файл: <code>{COOKIES_FILE}</code>, обновлён {_cookie_mtime()}" if COOKIES_FILE.exists() else "Куки-файл ещё не загружен."
        await update.message.reply_html(
            f"🍪 <b>Обновление cookies.txt</b>\n\n"
            f"{current}\n\n"
            f"Прикрепите <code>cookies.txt</code> и отправьте с подписью <code>/cookie</code>."
        )
        return

    # Validate: must be a plain-text file with a recognisable name
    if doc.mime_type not in ("text/plain", "application/octet-stream") and not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Пришлите файл в формате .txt (Netscape cookies format).")
        return

    tg_file = await doc.get_file()
    await tg_file.download_to_drive(str(COOKIES_FILE))

    logger.info("cookies.txt updated by admin (user_id=%d)", ADMIN_USER_ID)
    await update.message.reply_html(
        f"✅ <b>cookies.txt обновлён.</b>\n"
        f"Размер: {COOKIES_FILE.stat().st_size // 1024} КБ\n"
        f"Новые куки будут использованы при следующем скачивании."
    )


def _cookie_mtime() -> str:
    """Human-readable last-modified time of cookies.txt."""
    import datetime
    ts = COOKIES_FILE.stat().st_mtime
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_text = update.inline_query.query.strip()
    logger.info("inline_query from user=%d: %r", update.effective_user.id, query_text)
    url = _extract_youtube_url(query_text)

    if not url:
        await update.inline_query.answer(
            [],
            cache_time=5,
            button=InlineQueryResultsButton(
                text="Вставьте ссылку на YouTube видео",
                start_parameter="help",
            ),
        )
        return

    video_id = _get_video_id(url)
    if not video_id:
        await update.inline_query.answer([], cache_time=5)
        return

    # Fetch metadata on first encounter
    if video_id not in _video_cache:
        info = await _fetch_video_info(url)
        if not info:
            await update.inline_query.answer([], cache_time=5)
            return
        _video_cache[video_id] = {
            "url": url,
            "title": info.get("title", "YouTube Video"),
            "thumbnail": info.get("thumbnail"),
            "sizes": _estimate_sizes(info),
        }

    cached = _video_cache[video_id]
    title = cached["title"]
    thumbnail = cached.get("thumbnail")

    # reply_markup is required to receive inline_message_id in chosen_inline_result
    loading_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏳ Загружаю...", callback_data="loading")
    ]])

    sizes = cached.get("sizes", {})
    results = []
    for opt in QUALITY_OPTIONS:
        cache_key = f"{video_id}|{opt['key']}"
        estimated = sizes.get(opt["key"])

        # Skip options that are definitely too large for Telegram
        if estimated and estimated > MAX_FILE_SIZE:
            mb = estimated // (1024 * 1024)
            results.append(InlineQueryResultArticle(
                id=f"{cache_key}|toobig",
                title=f"⛔ {opt['label']}  (~{mb} МБ)",
                description="Превышает лимит Telegram 50 МБ",
                thumbnail_url=thumbnail,
                input_message_content=InputTextMessageContent(
                    f"⛔ <b>{opt['label']} недоступно</b>\n\n"
                    f"Размер ~{mb} МБ превышает лимит Telegram (50 МБ).\n\n"
                    f"<i>{title}</i>",
                    parse_mode=ParseMode.HTML,
                ),
            ))
            continue

        is_cached = cache_key in _file_cache
        icon = "⚡" if is_cached else "⏳"
        hint = "уже скачано" if is_cached else "нужно скачать"
        size_hint = f"  •  ~{estimated // (1024 * 1024)} МБ" if estimated else ""
        results.append(InlineQueryResultArticle(
            id=cache_key,
            title=f"{icon} {opt['label']}",
            description=f"{title}  •  {hint}{size_hint}",
            thumbnail_url=thumbnail,
            input_message_content=InputTextMessageContent(
                f"{'⚡' if is_cached else '⏳'} <b>{'Отправляю...' if is_cached else 'Загружаю...'}</b>\n\n"
                f"🎬 {title}\n"
                f"📊 {opt['label']}",
                parse_mode=ParseMode.HTML,
            ),
            reply_markup=loading_markup,
        ))

    await update.inline_query.answer(results, cache_time=60)


async def chosen_inline_result_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chosen = update.chosen_inline_result
    inline_message_id = chosen.inline_message_id
    logger.info("chosen_inline_result: result_id=%r inline_message_id=%r", chosen.result_id, inline_message_id)

    if not inline_message_id or "|" not in chosen.result_id:
        logger.warning("chosen_inline_result: missing inline_message_id or bad result_id, skipping")
        return

    if chosen.result_id.endswith("|toobig"):
        return  # user tapped an oversized option, message already shows the error

    video_id, quality_key = chosen.result_id.split("|", 1)
    opt = _quality_map.get(quality_key)
    cached = _video_cache.get(video_id)

    if not opt:
        logger.warning("chosen_inline_result: unknown quality key %r, skipping", quality_key)
        return

    if not cached:
        # Cache miss: bot restarted or fetch hasn't finished yet — reconstruct from video_id
        logger.info("chosen_inline_result: cache miss for %s, fetching info now", video_id)
        url = f"https://www.youtube.com/watch?v={video_id}"
        info = await _fetch_video_info(url)
        if not info:
            return
        cached = {
            "url": url,
            "title": info.get("title", "YouTube Video"),
            "thumbnail": info.get("thumbnail"),
        }
        _video_cache[video_id] = cached

    logger.info("Starting download: video_id=%s quality=%s", video_id, quality_key)
    # Fire-and-forget: download runs in background while the "Loading..." message is live
    asyncio.create_task(
        _download_and_update(
            context,
            inline_message_id=inline_message_id,
            url=cached["url"],
            title=cached["title"],
            opt=opt,
        )
    )


# ---------------------------------------------------------------------------
# Download + upload logic
# ---------------------------------------------------------------------------

async def _download_and_update(
    context,
    *,
    inline_message_id: str,
    url: str,
    title: str,
    opt: dict,
) -> None:
    is_audio: bool = opt["is_audio"]
    loop = asyncio.get_running_loop()
    cache_key = f"{_get_video_id(url)}|{opt['key']}"
    logger.info("_download_and_update started: url=%s quality=%s", url, opt["key"])

    # --- Cache hit: skip download entirely ---
    if cache_key in _file_cache:
        logger.info("Cache hit for %s, sending directly", cache_key)
        file_id = _file_cache[cache_key]
        try:
            if is_audio:
                media = InputMediaAudio(media=file_id, title=title, performer="YouTube")
            else:
                media = InputMediaVideo(media=file_id, supports_streaming=True)
            await context.bot.edit_message_media(
                inline_message_id=inline_message_id, media=media
            )
        except TelegramError as exc:
            logger.warning("Cached file_id rejected (%s), falling through to download", exc)
            del _file_cache[cache_key]
            _save_file_cache()
        else:
            return

    with tempfile.TemporaryDirectory() as tmpdir:
        out_template = os.path.join(tmpdir, "media.%(ext)s")

        ydl_opts: dict = {
            "format": opt["format"],
            "outtmpl": out_template,
            "quiet": True,
            "no_warnings": True,
            **_ytdlp_auth_opts(),
        }
        if is_audio:
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        else:
            ydl_opts["merge_output_format"] = "mp4"

        def _run_download() -> str | None:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    ydl.download([url])
                except Exception as exc:
                    logger.error("yt-dlp download error: %s", exc)
                    return None
            files = sorted(Path(tmpdir).iterdir())
            return str(files[0]) if files else None

        filepath = await loop.run_in_executor(None, _run_download)

        if not filepath or not os.path.exists(filepath):
            await _edit_error(context, inline_message_id, "Не удалось скачать файл.", title)
            return

        file_size = os.path.getsize(filepath)
        if file_size > MAX_FILE_SIZE:
            mb = file_size // (1024 * 1024)
            await _edit_error(
                context,
                inline_message_id,
                f"Файл слишком большой: {mb} МБ.\nTelegram ограничивает размер файла до 50 МБ.",
                title,
            )
            return

        try:
            # Step 1: upload to storage chat to get a Telegram file_id
            with open(filepath, "rb") as fh:
                if is_audio:
                    sent = await context.bot.send_audio(
                        chat_id=STORAGE_CHAT_ID,
                        audio=fh,
                        title=title,
                        performer="YouTube",
                        write_timeout=300,
                        read_timeout=300,
                    )
                    _file_cache[cache_key] = sent.audio.file_id
                    _save_file_cache()
                    media = InputMediaAudio(
                        media=sent.audio.file_id,
                        title=title,
                        performer="YouTube",
                    )
                else:
                    sent = await context.bot.send_video(
                        chat_id=STORAGE_CHAT_ID,
                        video=fh,
                        supports_streaming=True,
                        write_timeout=300,
                        read_timeout=300,
                    )
                    _file_cache[cache_key] = sent.video.file_id
                    _save_file_cache()
                    media = InputMediaVideo(
                        media=sent.video.file_id,
                        supports_streaming=True,
                    )

            # Step 2: replace the "Loading..." inline message with the actual media
            await context.bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=media,
            )

        except TelegramError as exc:
            logger.error("Telegram error during upload/edit: %s", exc)
            await _edit_error(context, inline_message_id, "Ошибка при отправке файла.", title)
        except Exception as exc:
            logger.error("Unexpected error: %s", exc)
            await _edit_error(context, inline_message_id, "Неизвестная ошибка.", title)


async def _edit_error(
    context,
    inline_message_id: str,
    reason: str,
    title: str,
) -> None:
    try:
        await context.bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=f"❌ <b>{reason}</b>\n\n<i>{title}</i>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        logger.error("Failed to edit error message: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _acquire_pid_lock() -> None:
    """Write PID file; exit if another instance is already running."""
    pid_file = Path("bot.pid")
    if pid_file.exists():
        old_pid = int(pid_file.read_text().strip())
        try:
            os.kill(old_pid, 0)  # check if process exists
            logger.error("Another instance is already running (PID %d). Exiting.", old_pid)
            sys.exit(1)
        except ProcessLookupError:
            pass  # stale PID file — overwrite it
    pid_file.write_text(str(os.getpid()))


def main() -> None:
    _acquire_pid_lock()
    _load_file_cache()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    # /cookie works both as a standalone command and as a caption on a document
    app.add_handler(CommandHandler("cookie", cmd_cookie))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.Caption(["/cookie"]), cmd_cookie))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(ChosenInlineResultHandler(chosen_inline_result_handler))

    # Auto-update yt-dlp once every 24 hours
    app.job_queue.run_repeating(_auto_update_ytdlp, interval=24 * 3600, first=24 * 3600)

    logger.info("Bot is running… (PID %d)", os.getpid())
    if COOKIES_FILE.exists():
        logger.info("Using cookies from %s", COOKIES_FILE)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        Path("bot.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
