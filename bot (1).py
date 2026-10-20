import os
import re
import asyncio
import logging
import tempfile
import shutil

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN = os.environ["BOT_TOKEN"]
MAX_MB = 50  # Telegram bot upload limit
URL_RE = re.compile(r"https?://\S+")
CHANNEL_RE = re.compile(r"youtube\.com/(@|channel/|c/|user/)", re.I)


def download(url: str, mode: str, folder: str, height: int = 480):
    opts = {
        "outtmpl": f"{folder}/%(title).60s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "max_filesize": MAX_MB * 1024 * 1024,
    }
    if mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}
        ]
    else:
        opts["format"] = (
            f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={height}][ext=mp4]/b[height<={height}]/worst"
        )
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = os.listdir(folder)
    return os.path.join(folder, files[0]) if files else None


def list_videos(target: str, n: int = 8):
    opts = {"quiet": True, "extract_flat": True, "playlistend": n, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    return [
        (e["id"], e.get("title") or "Video")
        for e in (info.get("entries") or [])
        if e and e.get("id")
    ][:n]


def results_keyboard(items):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ " + t[:55], callback_data=f"pick:{vid}")] for vid, t in items]
    )


def format_keyboard():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🎬 Video", callback_data="video"),
            InlineKeyboardButton("🎵 Audio", callback_data="audio"),
        ]]
    )


def quality_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("360p", callback_data="q:360"),
                InlineKeyboardButton("480p", callback_data="q:480"),
            ],
            [
                InlineKeyboardButton("720p", callback_data="q:720"),
                InlineKeyboardButton("1080p", callback_data="q:1080"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me:\n"
        "• a video link → I download it\n"
        "• a YouTube channel link → I list its latest videos\n"
        "• any words (or /search words) → I search YouTube\n\n"
        "Only download content you have the right to save."
    )


async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    msg = await update.message.reply_text("🔎 Searching...")
    try:
        items = await asyncio.to_thread(list_videos, f"ytsearch5:{query}", 5)
    except Exception:
        log.exception("Search failed")
        items = []
    if not items:
        await msg.edit_text("No results found.")
        return
    await msg.edit_text("Choose a result:", reply_markup=results_keyboard(items))


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /search song or video name")
        return
    await do_search(update, context, query)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = URL_RE.search(text)
    if not match:
        await do_search(update, context, text)  # plain words = search
        return
    url = match.group(0)
    if CHANNEL_RE.search(url):
        msg = await update.message.reply_text("📺 Loading latest videos...")
        target = url.rstrip("/")
        if not target.endswith("/videos"):
            target += "/videos"
        try:
            items = await asyncio.to_thread(list_videos, target, 8)
        except Exception:
            log.exception("Channel failed")
            items = []
        if not items:
            await msg.edit_text("Could not read this channel.")
            return
        await msg.edit_text("Latest videos:", reply_markup=results_keyboard(items))
        return
    context.user_data["url"] = url
    await update.message.reply_text("Choose format:", reply_markup=format_keyboard())


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    vid = q.data.split(":", 1)[1]
    context.user_data["url"] = f"https://www.youtube.com/watch?v={vid}"
    await q.message.reply_text("Choose format:", reply_markup=format_keyboard())


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    url = context.user_data.get("url")
    if not url:
        await q.edit_message_text("Send the link again.")
        return
    if q.data == "video":
        await q.edit_message_text("Choose quality:", reply_markup=quality_keyboard())
        return
    if q.data == "audio":
        mode, height = "audio", 0
    else:
        mode, height = "video", int(q.data.split(":")[1])
    await q.edit_message_text("⏳ Downloading...")
    folder = tempfile.mkdtemp()
    try:
        path = await asyncio.to_thread(download, url, mode, folder, height)
        if not path or os.path.getsize(path) > MAX_MB * 1024 * 1024:
            await q.edit_message_text(
                "❌ File is over Telegram's 50 MB limit or unavailable. Try a lower quality."
            )
            return
        await q.edit_message_text("📤 Uploading...")
        with open(path, "rb") as f:
            if mode == "audio":
                await q.message.reply_audio(f, write_timeout=180)
            else:
                await q.message.reply_video(f, write_timeout=180)
        await q.delete_message()
    except Exception:
        log.exception("Download failed")
        await q.edit_message_text("❌ Could not download this link.")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^(video|audio|q:\d+)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
