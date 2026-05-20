"""
Elite Music Bot — Pyrogram + pytgcalls + yt-dlp
Single file deployment | cookies.txt support built-in

Requirements:
    pip install pyrogram tgcrypto pytgcalls yt-dlp

Run:
    python music_bot.py
"""

import asyncio
import os
import re
from collections import defaultdict

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ChatMemberStatus
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped, AudioVideoPiped
from pytgcalls.exceptions import NoActiveGroupCall

# ─────────────────────────────────────────────
#  CONFIG — edit these or use environment vars
# ─────────────────────────────────────────────
API_ID   = int(os.getenv("API_ID",   "0"))          # my.telegram.org
API_HASH = os.getenv("API_HASH",  "your_api_hash")
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_bot_token")

COOKIES_FILE = "cookies.txt"   # path to your cookies file
DOWNLOADS_DIR = "downloads"    # temp audio storage

# ─────────────────────────────────────────────
#  QUEUE STORAGE
#  queue[chat_id] = [ {title, url, requested_by}, ... ]
# ─────────────────────────────────────────────
queue: dict[int, list[dict]] = defaultdict(list)
current: dict[int, dict] = {}   # currently playing per chat

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ─────────────────────────────────────────────
#  PYROGRAM + PYTGCALLS CLIENTS
# ─────────────────────────────────────────────
app = Client("elite_music_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
calls = PyTgCalls(app)


# ─────────────────────────────────────────────
#  YT-DLP HELPERS
# ─────────────────────────────────────────────
def _ydl_opts(output_path: str) -> dict:
    opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "noplaylist": True,
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _ydl_info_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


async def search_and_download(query: str) -> dict | None:
    """Search YouTube, download audio. Returns info dict or None."""
    loop = asyncio.get_event_loop()

    # if not a URL, treat as search
    is_url = re.match(r"https?://", query)
    search_query = query if is_url else f"ytsearch1:{query}"

    # 1. get info
    def _get_info():
        with yt_dlp.YoutubeDL(_ydl_info_opts()) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if not is_url and "entries" in info:
                info = info["entries"][0]
            return info

    try:
        info = await loop.run_in_executor(None, _get_info)
    except Exception as e:
        print(f"[yt-dlp info error] {e}")
        return None

    video_id = info.get("id", info.get("display_id", "unknown"))
    title    = info.get("title", "Unknown Title")
    duration = info.get("duration", 0)
    webpage  = info.get("webpage_url", query)

    out_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.%(ext)s")
    final_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp3")

    # 2. download if not cached
    if not os.path.exists(final_path):
        def _download():
            with yt_dlp.YoutubeDL(_ydl_opts(out_path)) as ydl:
                ydl.download([webpage])

        try:
            await loop.run_in_executor(None, _download)
        except Exception as e:
            print(f"[yt-dlp download error] {e}")
            return None

    return {
        "title": title,
        "url": webpage,
        "file": final_path,
        "duration": duration,
        "video_id": video_id,
    }


def fmt_duration(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


async def is_admin(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


# ─────────────────────────────────────────────
#  PLAYBACK HELPERS
# ─────────────────────────────────────────────
async def play_next(chat_id: int):
    """Play next track in queue or leave if empty."""
    if not queue[chat_id]:
        current.pop(chat_id, None)
        try:
            await calls.leave_group_call(chat_id)
        except Exception:
            pass
        return

    track = queue[chat_id].pop(0)
    current[chat_id] = track

    try:
        await calls.change_stream(chat_id, AudioPiped(track["file"]))
    except NoActiveGroupCall:
        await calls.join_group_call(chat_id, AudioPiped(track["file"]), stream_type=None)
    except Exception as e:
        print(f"[play_next error] {e}")
        await play_next(chat_id)


# ─────────────────────────────────────────────
#  PYTGCALLS CALLBACK — stream ended
# ─────────────────────────────────────────────
@calls.on_stream_end()
async def on_stream_end(_, update):
    chat_id = update.chat_id
    await play_next(chat_id)


# ─────────────────────────────────────────────
#  BOT COMMANDS
# ─────────────────────────────────────────────

@app.on_message(filters.command(["start", "help"]))
async def cmd_start(_, msg: Message):
    text = (
        "🎵 **Elite Music Bot**\n\n"
        "**Commands:**\n"
        "`/play <song name or URL>` — Play / add to queue\n"
        "`/skip` — Skip current track\n"
        "`/pause` — Pause playback\n"
        "`/resume` — Resume playback\n"
        "`/stop` — Stop & clear queue\n"
        "`/queue` — Show queue\n"
        "`/np` — Now playing\n"
        "`/ping` — Bot latency check\n\n"
        "🍪 Cookies loaded: `{}`".format("✅ Yes" if os.path.exists(COOKIES_FILE) else "❌ No")
    )
    await msg.reply(text)


@app.on_message(filters.command("play") & filters.group)
async def cmd_play(_, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("❌ Usage: `/play <song name or YouTube URL>`")

    query = " ".join(msg.command[1:])
    sent  = await msg.reply(f"🔍 Searching: **{query}**...")

    track = await search_and_download(query)
    if not track:
        return await sent.edit("❌ Could not find or download that track. Check cookies.txt?")

    track["requested_by"] = msg.from_user.mention if msg.from_user else "Unknown"
    chat_id = msg.chat.id

    # If already playing, add to queue
    if chat_id in current:
        queue[chat_id].append(track)
        pos = len(queue[chat_id])
        return await sent.edit(
            f"➕ **Added to Queue** [#{pos}]\n"
            f"🎵 {track['title']}\n"
            f"⏱ {fmt_duration(track['duration'])}"
        )

    # Start fresh
    current[chat_id] = track
    try:
        await calls.join_group_call(chat_id, AudioPiped(track["file"]))
        await sent.edit(
            f"▶️ **Now Playing**\n"
            f"🎵 {track['title']}\n"
            f"⏱ {fmt_duration(track['duration'])}\n"
            f"👤 {track['requested_by']}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⏭ Skip", callback_data="skip"),
                    InlineKeyboardButton("⏸ Pause", callback_data="pause"),
                    InlineKeyboardButton("⏹ Stop", callback_data="stop"),
                ]
            ])
        )
    except NoActiveGroupCall:
        current.pop(chat_id, None)
        await sent.edit("❌ No active voice chat found. Please start a Voice Chat first!")
    except Exception as e:
        current.pop(chat_id, None)
        await sent.edit(f"❌ Error: `{e}`")


@app.on_message(filters.command("skip") & filters.group)
async def cmd_skip(_, msg: Message):
    chat_id = msg.chat.id
    if chat_id not in current:
        return await msg.reply("❌ Nothing is playing.")
    await play_next(chat_id)
    await msg.reply("⏭ Skipped!")


@app.on_message(filters.command("pause") & filters.group)
async def cmd_pause(_, msg: Message):
    try:
        await calls.pause_stream(msg.chat.id)
        await msg.reply("⏸ Paused.")
    except Exception as e:
        await msg.reply(f"❌ `{e}`")


@app.on_message(filters.command("resume") & filters.group)
async def cmd_resume(_, msg: Message):
    try:
        await calls.resume_stream(msg.chat.id)
        await msg.reply("▶️ Resumed.")
    except Exception as e:
        await msg.reply(f"❌ `{e}`")


@app.on_message(filters.command("stop") & filters.group)
async def cmd_stop(_, msg: Message):
    chat_id = msg.chat.id
    queue[chat_id].clear()
    current.pop(chat_id, None)
    try:
        await calls.leave_group_call(chat_id)
    except Exception:
        pass
    await msg.reply("⏹ Stopped and left voice chat.")


@app.on_message(filters.command("queue") & filters.group)
async def cmd_queue(_, msg: Message):
    chat_id = msg.chat.id
    q = queue[chat_id]
    now = current.get(chat_id)

    if not now and not q:
        return await msg.reply("📭 Queue is empty.")

    lines = []
    if now:
        lines.append(f"▶️ **Now:** {now['title']} ({fmt_duration(now['duration'])})")
    for i, t in enumerate(q, 1):
        lines.append(f"{i}. {t['title']} ({fmt_duration(t['duration'])}) — {t['requested_by']}")

    await msg.reply("\n".join(lines))


@app.on_message(filters.command("np") & filters.group)
async def cmd_np(_, msg: Message):
    now = current.get(msg.chat.id)
    if not now:
        return await msg.reply("❌ Nothing is playing right now.")
    await msg.reply(
        f"🎵 **Now Playing**\n"
        f"**{now['title']}**\n"
        f"⏱ {fmt_duration(now['duration'])}\n"
        f"👤 {now.get('requested_by', 'Unknown')}"
    )


@app.on_message(filters.command("ping"))
async def cmd_ping(_, msg: Message):
    import time
    s = time.time()
    m = await msg.reply("🏓 Pong!")
    await m.edit(f"🏓 Pong! `{round((time.time()-s)*1000)}ms`")


# ─────────────────────────────────────────────
#  INLINE BUTTON CALLBACKS
# ─────────────────────────────────────────────
@app.on_callback_query(filters.regex("^(skip|pause|stop)$"))
async def cb_controls(_, cq):
    chat_id = cq.message.chat.id
    action  = cq.data

    if action == "skip":
        await play_next(chat_id)
        await cq.answer("⏭ Skipped!")
    elif action == "pause":
        try:
            await calls.pause_stream(chat_id)
            await cq.answer("⏸ Paused!")
        except Exception as e:
            await cq.answer(f"Error: {e}", show_alert=True)
    elif action == "stop":
        queue[chat_id].clear()
        current.pop(chat_id, None)
        try:
            await calls.leave_group_call(chat_id)
        except Exception:
            pass
        await cq.answer("⏹ Stopped!")
        await cq.message.edit_text("⏹ Playback stopped.")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def main():
    print("🎵 Elite Music Bot starting...")
    cookies_status = "✅ Loaded" if os.path.exists(COOKIES_FILE) else "⚠️  Not found (age-restricted content may fail)"
    print(f"🍪 cookies.txt: {cookies_status}")
    await calls.start()
    print("✅ Bot is running!")
    await asyncio.Event().wait()


if __name__ == "__main__":
    with app:
        app.loop.run_until_complete(main())
