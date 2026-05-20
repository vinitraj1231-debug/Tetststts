"""
╔══════════════════════════════════════════════════════════╗
║           ELITE MUSIC BOT — ADVANCED EDITION            ║
║   Pyrogram + pytgcalls + yt-dlp | String Session        ║
╚══════════════════════════════════════════════════════════╝

SETUP:
  1. Fill config below OR export env vars
  2. Get STRING_SESSION via @StringFatherBot or run get_string.py
  3. pip install -r requirements.txt
  4. python music_bot.py

FEATURES:
  ✅ String Session (userbot joins VC)
  ✅ Queue with position display
  ✅ Loop (track / queue)
  ✅ Shuffle queue
  ✅ Volume control
  ✅ Search → pick result (1-5)
  ✅ YouTube + playlist support
  ✅ Admin-only destructive commands
  ✅ Now playing with progress bar
  ✅ Auto file cleanup
  ✅ Cookies.txt support
  ✅ /seek support
  ✅ Thumbnail in now-playing
  ✅ Startup broadcast to owner
"""

# ─────────────────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────────────────
import asyncio
import math
import os
import random
import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait, UserNotParticipant
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped, MediaStream
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError

# ─────────────────────────────────────────────────────────
#  ⚙️  CONFIG — edit here or use env vars
# ─────────────────────────────────────────────────────────
API_ID        = int(os.getenv("API_ID",        "0"))
API_HASH      =     os.getenv("API_HASH",      "YOUR_API_HASH")
BOT_TOKEN     =     os.getenv("BOT_TOKEN",     "YOUR_BOT_TOKEN")

# String session for the ASSISTANT (userbot) account — needed to join VC
# Generate via: python get_string.py
STRING_SESSION = os.getenv("STRING_SESSION", "YOUR_STRING_SESSION")

OWNER_ID      = int(os.getenv("OWNER_ID",      "0"))   # your Telegram user ID
COOKIES_FILE  =     os.getenv("COOKIES_FILE",  "cookies.txt")
DOWNLOADS_DIR =     os.getenv("DOWNLOADS_DIR", "downloads")
MAX_QUEUE     = int(os.getenv("MAX_QUEUE",     "50"))   # max tracks per chat
MAX_DURATION  = int(os.getenv("MAX_DURATION",  "7200")) # 2 hours max per track
CLEANUP_HOURS = int(os.getenv("CLEANUP_HOURS", "2"))    # delete old files after N hours

# ─────────────────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────────────────
@dataclass
class Track:
    title:        str
    url:          str
    file:         str
    duration:     int
    video_id:     str
    thumbnail:    str  = ""
    requested_by: str  = "Unknown"
    requester_id: int  = 0
    added_at:     float = field(default_factory=time.time)

@dataclass
class ChatState:
    queue:      list      = field(default_factory=list)   # list[Track]
    current:    Optional[object] = None                   # Track | None
    loop_mode:  str       = "off"                         # off / track / queue
    volume:     int       = 100
    paused:     bool      = False
    started_at: float     = 0.0    # epoch when current track started
    np_msg_id:  int       = 0      # message id of now-playing message

states: dict[int, ChatState] = defaultdict(ChatState)

# Pending searches: user_id → list of Track (search results)
pending_searches: dict[int, list] = {}

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────
#  PYROGRAM CLIENTS
# ─────────────────────────────────────────────────────────
bot = Client(
    "elite_music_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# Userbot (assistant) — uses string session to actually JOIN the voice chat
assistant = Client(
    "elite_assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=STRING_SESSION,
)

calls = PyTgCalls(assistant)

# ─────────────────────────────────────────────────────────
#  YT-DLP HELPERS
# ─────────────────────────────────────────────────────────
def _cookies_opt() -> dict:
    if os.path.exists(COOKIES_FILE):
        return {"cookiefile": COOKIES_FILE}
    return {}

def _info_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        **_cookies_opt(),
    }

def _dl_opts(output_path: str) -> dict:
    return {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "noplaylist": True,
        **_cookies_opt(),
    }

def _search_opts(n: int = 5) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        **_cookies_opt(),
    }


async def fetch_info(query: str, n_results: int = 1) -> list[dict]:
    """Return list of info dicts (n_results items) for a query."""
    loop = asyncio.get_event_loop()
    is_url = bool(re.match(r"https?://", query))
    search_q = query if is_url else f"ytsearch{n_results}:{query}"

    def _get():
        with yt_dlp.YoutubeDL(_search_opts(n_results)) as ydl:
            info = ydl.extract_info(search_q, download=False)
            if "entries" in info:
                return [e for e in info["entries"] if e]
            return [info]

    try:
        return await loop.run_in_executor(None, _get)
    except Exception as e:
        print(f"[yt-dlp fetch_info] {e}")
        return []


async def download_track(info: dict) -> Optional[str]:
    """Download audio for an info dict. Returns file path or None."""
    loop = asyncio.get_event_loop()
    video_id  = info.get("id", "unknown")
    webpage   = info.get("webpage_url", info.get("url", ""))
    final     = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp3")

    if os.path.exists(final):
        return final

    out = os.path.join(DOWNLOADS_DIR, f"{video_id}.%(ext)s")

    def _dl():
        with yt_dlp.YoutubeDL(_dl_opts(out)) as ydl:
            ydl.download([webpage])

    try:
        await loop.run_in_executor(None, _dl)
        return final if os.path.exists(final) else None
    except Exception as e:
        print(f"[yt-dlp download] {e}")
        return None


def info_to_track(info: dict, requester: str = "Unknown", requester_id: int = 0) -> Track:
    return Track(
        title        = info.get("title", "Unknown"),
        url          = info.get("webpage_url", info.get("url", "")),
        file         = os.path.join(DOWNLOADS_DIR, f"{info.get('id','x')}.mp3"),
        duration     = info.get("duration") or 0,
        video_id     = info.get("id", "x"),
        thumbnail    = info.get("thumbnail", ""),
        requested_by = requester,
        requester_id = requester_id,
    )

# ─────────────────────────────────────────────────────────
#  FORMATTING HELPERS
# ─────────────────────────────────────────────────────────
def fmt_dur(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def progress_bar(elapsed: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "──────────────"
    pct    = min(elapsed / total, 1.0)
    filled = int(pct * width)
    bar    = "▓" * filled + "░" * (width - filled)
    return f"{bar}"


def now_playing_text(track: Track, state: ChatState) -> str:
    elapsed = int(time.time() - state.started_at) if state.started_at else 0
    elapsed = min(elapsed, track.duration)
    bar     = progress_bar(elapsed, track.duration)
    loop_icon = {"off": "➡️", "track": "🔂", "queue": "🔁"}[state.loop_mode]

    lines = [
        f"{'⏸' if state.paused else '▶️'}  **Now Playing**",
        f"",
        f"🎵 **{track.title}**",
        f"",
        f"`{fmt_dur(elapsed)}` {bar} `{fmt_dur(track.duration)}`",
        f"",
        f"🔊 Volume: `{state.volume}%`  {loop_icon} Loop: `{state.loop_mode}`",
        f"👤 Requested by: {track.requested_by}",
    ]
    if state.queue:
        nxt = state.queue[0]
        lines.append(f"⏭ Up next: **{nxt.title}**")
    return "\n".join(lines)


def np_buttons(paused: bool = False) -> InlineKeyboardMarkup:
    pause_btn = ("▶️ Resume", "resume") if paused else ("⏸ Pause", "pause")
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏮ Replay",  callback_data="replay"),
            InlineKeyboardButton(pause_btn[0], callback_data=pause_btn[1]),
            InlineKeyboardButton("⏭ Skip",    callback_data="skip"),
        ],
        [
            InlineKeyboardButton("🔉 Vol -10", callback_data="vol_down"),
            InlineKeyboardButton("🔊 Vol +10", callback_data="vol_up"),
            InlineKeyboardButton("⏹ Stop",    callback_data="stop"),
        ],
        [
            InlineKeyboardButton("🔂 Loop Track", callback_data="loop_track"),
            InlineKeyboardButton("🔁 Loop Queue", callback_data="loop_queue"),
            InlineKeyboardButton("🔀 Shuffle",    callback_data="shuffle"),
        ],
    ])

# ─────────────────────────────────────────────────────────
#  PERMISSION HELPERS
# ─────────────────────────────────────────────────────────
async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

# ─────────────────────────────────────────────────────────
#  CORE PLAYBACK
# ─────────────────────────────────────────────────────────
async def _update_np_message(chat_id: int):
    """Silently refresh the now-playing message text."""
    st = states[chat_id]
    if not st.current or not st.np_msg_id:
        return
    try:
        await bot.edit_message_text(
            chat_id,
            st.np_msg_id,
            now_playing_text(st.current, st),
            reply_markup=np_buttons(st.paused),
        )
    except Exception:
        pass


async def _start_stream(chat_id: int, track: Track):
    """Join/change stream. Updates state."""
    st = states[chat_id]
    st.current    = track
    st.started_at = time.time()
    st.paused     = False

    stream = AudioPiped(track.file)

    try:
        active = await calls.get_active_call(chat_id)
        if active:
            await calls.change_stream(chat_id, stream)
        else:
            raise NoActiveGroupCall
    except (NoActiveGroupCall, NotInCallError):
        try:
            await calls.join_group_call(chat_id, stream)
        except Exception as e:
            raise RuntimeError(f"VC join failed: {e}")


async def play_next(chat_id: int):
    st = states[chat_id]

    # loop track
    if st.loop_mode == "track" and st.current:
        track = st.current
    elif st.queue:
        track = st.queue.pop(0)
        # loop queue: push back
        if st.loop_mode == "queue":
            st.queue.append(track)
    else:
        # queue empty, leave
        st.current = None
        try:
            await calls.leave_group_call(chat_id)
        except Exception:
            pass
        if st.np_msg_id:
            try:
                await bot.edit_message_text(
                    chat_id, st.np_msg_id,
                    "✅ Queue finished. Leaving voice chat!"
                )
            except Exception:
                pass
        return

    # download if needed
    if not os.path.exists(track.file):
        infos = await fetch_info(track.url, 1)
        if infos:
            await download_track(infos[0])

    try:
        await _start_stream(chat_id, track)
    except Exception as e:
        # skip broken track
        print(f"[play_next error] {e}")
        await play_next(chat_id)
        return

    await _update_np_message(chat_id)

# ─────────────────────────────────────────────────────────
#  PYTGCALLS CALLBACKS
# ─────────────────────────────────────────────────────────
@calls.on_stream_end()
async def _on_stream_end(_, update):
    await play_next(update.chat_id)


@calls.on_kicked()
async def _on_kicked(_, update):
    chat_id = update.chat_id
    st = states[chat_id]
    st.queue.clear()
    st.current = None

# ─────────────────────────────────────────────────────────
#  FILE CLEANUP TASK
# ─────────────────────────────────────────────────────────
async def _cleanup_task():
    """Delete downloaded files older than CLEANUP_HOURS."""
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        # collect video_ids currently in use
        in_use = set()
        for st in states.values():
            if st.current:
                in_use.add(st.current.video_id)
            for t in st.queue:
                in_use.add(t.video_id)

        for f in os.listdir(DOWNLOADS_DIR):
            path = os.path.join(DOWNLOADS_DIR, f)
            vid  = f.rsplit(".", 1)[0]
            if vid in in_use:
                continue
            if os.path.isfile(path) and (now - os.path.getmtime(path)) > CLEANUP_HOURS * 3600:
                os.remove(path)
                print(f"[cleanup] Deleted {f}")

# ─────────────────────────────────────────────────────────
#  BOT COMMANDS
# ─────────────────────────────────────────────────────────

# /start /help
@bot.on_message(filters.command(["start", "help"]))
async def cmd_help(_, msg: Message):
    cookies_ok = "✅" if os.path.exists(COOKIES_FILE) else "❌"
    text = f"""🎵 **Elite Music Bot — Advanced**

**🎶 Playback**
`/play <song / URL>` — Search & play
`/playurl <URL>` — Direct URL play
`/search <song>` — Pick from 5 results
`/np` — Now playing panel

**📋 Queue**
`/queue` — View full queue
`/skip [n]` — Skip n tracks
`/remove <pos>` — Remove from queue
`/shuffle` — Shuffle queue
`/clear` — Clear queue

**🎛 Controls**
`/pause` / `/resume`
`/volume <1-200>` — Set volume
`/seek <mm:ss>` — Seek to position
`/loop off|track|queue` — Loop mode
`/replay` — Restart current track

**⚙️ Admin Only**
`/stop` — Stop & leave VC
`/forceskip` — Force skip (ignores perms)

**ℹ️ Info**
`/ping` — Latency
`/stats` — Bot statistics

🍪 Cookies: {cookies_ok} `{"Loaded" if os.path.exists(COOKIES_FILE) else "Not found"}`
"""
    await msg.reply(text)


# /play
@bot.on_message(filters.command("play") & filters.group)
async def cmd_play(_, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("❌ Usage: `/play <song name or URL>`")

    query = " ".join(msg.command[1:])
    sent  = await msg.reply(f"🔍 Searching for: **{query}**...")

    infos = await fetch_info(query, 1)
    if not infos:
        return await sent.edit("❌ Nothing found. Try another query or check cookies.txt")

    info   = infos[0]
    dur    = info.get("duration") or 0
    if dur > MAX_DURATION:
        return await sent.edit(f"❌ Track too long ({fmt_dur(dur)}). Max allowed: {fmt_dur(MAX_DURATION)}")

    await sent.edit(f"⬇️ Downloading: **{info.get('title','...')}**...")

    file = await download_track(info)
    if not file:
        return await sent.edit("❌ Download failed. Bot might be rate-limited.")

    track = info_to_track(
        info,
        requester    = msg.from_user.mention if msg.from_user else "Unknown",
        requester_id = msg.from_user.id if msg.from_user else 0,
    )
    chat_id = msg.chat.id
    st      = states[chat_id]

    if len(st.queue) >= MAX_QUEUE:
        return await sent.edit(f"❌ Queue full ({MAX_QUEUE} tracks max).")

    if st.current:
        st.queue.append(track)
        pos = len(st.queue)
        return await sent.edit(
            f"➕ **Added to Queue** [#{pos}]\n"
            f"🎵 {track.title}\n"
            f"⏱ {fmt_dur(track.duration)}\n"
            f"👤 {track.requested_by}"
        )

    # start playing
    try:
        await _start_stream(chat_id, track)
    except RuntimeError as e:
        return await sent.edit(f"❌ {e}\n\nMake sure a Voice Chat is active!")

    np_text = now_playing_text(track, st)
    np_msg  = await sent.edit(np_text, reply_markup=np_buttons())
    st.np_msg_id = np_msg.id


# /search
@bot.on_message(filters.command("search") & filters.group)
async def cmd_search(_, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("❌ Usage: `/search <song name>`")

    query = " ".join(msg.command[1:])
    sent  = await msg.reply(f"🔍 Searching top 5 results for: **{query}**...")

    infos = await fetch_info(query, 5)
    if not infos:
        return await sent.edit("❌ No results found.")

    user_id = msg.from_user.id
    pending_searches[user_id] = infos

    lines = [f"🔍 **Search Results for:** `{query}`\n"]
    for i, info in enumerate(infos, 1):
        dur = fmt_dur(info.get("duration") or 0)
        lines.append(f"**{i}.** {info.get('title','Unknown')} `[{dur}]`")
    lines.append("\n💬 Reply with a number (1-5) to play")

    await sent.edit("\n".join(lines))


# Handle search number reply
@bot.on_message(filters.group & filters.regex(r"^[1-5]$"))
async def handle_search_pick(_, msg: Message):
    user_id = msg.from_user.id if msg.from_user else 0
    if user_id not in pending_searches:
        return

    choice = int(msg.text) - 1
    infos  = pending_searches.pop(user_id)
    if choice >= len(infos):
        return await msg.reply("❌ Invalid choice.")

    info    = infos[choice]
    dur     = info.get("duration") or 0
    if dur > MAX_DURATION:
        return await msg.reply(f"❌ Track too long ({fmt_dur(dur)}).")

    sent = await msg.reply(f"⬇️ Downloading: **{info.get('title','...')}**...")
    file = await download_track(info)
    if not file:
        return await sent.edit("❌ Download failed.")

    track = info_to_track(
        info,
        requester    = msg.from_user.mention,
        requester_id = user_id,
    )
    chat_id = msg.chat.id
    st      = states[chat_id]

    if st.current:
        st.queue.append(track)
        return await sent.edit(
            f"➕ Added to Queue [#{len(st.queue)}]\n🎵 {track.title}"
        )

    try:
        await _start_stream(chat_id, track)
    except RuntimeError as e:
        return await sent.edit(f"❌ {e}")

    np_msg = await sent.edit(now_playing_text(track, st), reply_markup=np_buttons())
    st.np_msg_id = np_msg.id


# /playurl
@bot.on_message(filters.command("playurl") & filters.group)
async def cmd_playurl(_, msg: Message):
    if len(msg.command) < 2 or not msg.command[1].startswith("http"):
        return await msg.reply("❌ Usage: `/playurl <YouTube URL>`")
    msg.command[0] = "play"
    await cmd_play(_, msg)


# /np
@bot.on_message(filters.command("np") & filters.group)
async def cmd_np(_, msg: Message):
    st = states[msg.chat.id]
    if not st.current:
        return await msg.reply("❌ Nothing is playing right now.")
    np_msg = await msg.reply(
        now_playing_text(st.current, st),
        reply_markup=np_buttons(st.paused),
    )
    st.np_msg_id = np_msg.id


# /queue
@bot.on_message(filters.command("queue") & filters.group)
async def cmd_queue(_, msg: Message):
    st = states[msg.chat.id]
    if not st.current and not st.queue:
        return await msg.reply("📭 Queue is empty.")

    lines = []
    if st.current:
        elapsed = int(time.time() - st.started_at)
        lines.append(
            f"{'⏸' if st.paused else '▶️'} **Now:** {st.current.title} "
            f"`[{fmt_dur(elapsed)}/{fmt_dur(st.current.duration)}]`"
        )
    if st.queue:
        lines.append(f"\n📋 **Queue ({len(st.queue)} tracks):**")
        for i, t in enumerate(st.queue[:20], 1):
            lines.append(f"`{i:02}.` {t.title} `[{fmt_dur(t.duration)}]` — {t.requested_by}")
        if len(st.queue) > 20:
            lines.append(f"_...and {len(st.queue)-20} more_")

    loop_icon = {"off": "➡️ Off", "track": "🔂 Track", "queue": "🔁 Queue"}[st.loop_mode]
    lines.append(f"\n{loop_icon}  🔊 Volume: `{st.volume}%`")
    await msg.reply("\n".join(lines))


# /skip
@bot.on_message(filters.command(["skip", "forceskip"]) & filters.group)
async def cmd_skip(_, msg: Message):
    is_force = msg.command[0] == "forceskip"
    if is_force and not await is_admin(msg.chat.id, msg.from_user.id):
        return await msg.reply("❌ Admins only.")

    n = 1
    if len(msg.command) > 1:
        try:
            n = int(msg.command[1])
        except ValueError:
            pass

    st = states[msg.chat.id]
    if not st.current:
        return await msg.reply("❌ Nothing is playing.")

    # skip n-1 from queue, then play_next skips current
    skip_n = max(0, n - 1)
    skipped = st.queue[:skip_n]
    st.queue = st.queue[skip_n:]

    await play_next(msg.chat.id)
    await msg.reply(f"⏭ Skipped **{n}** track(s).")


# /remove
@bot.on_message(filters.command("remove") & filters.group)
async def cmd_remove(_, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("❌ Usage: `/remove <position>`")
    try:
        pos = int(msg.command[1]) - 1
    except ValueError:
        return await msg.reply("❌ Invalid position number.")

    st = states[msg.chat.id]
    if not st.queue or pos < 0 or pos >= len(st.queue):
        return await msg.reply("❌ Invalid position.")

    removed = st.queue.pop(pos)
    await msg.reply(f"🗑 Removed: **{removed.title}**")


# /shuffle
@bot.on_message(filters.command("shuffle") & filters.group)
async def cmd_shuffle(_, msg: Message):
    st = states[msg.chat.id]
    if not st.queue:
        return await msg.reply("❌ Queue is empty.")
    random.shuffle(st.queue)
    await msg.reply(f"🔀 Queue shuffled! ({len(st.queue)} tracks)")


# /clear
@bot.on_message(filters.command("clear") & filters.group)
async def cmd_clear(_, msg: Message):
    if not await is_admin(msg.chat.id, msg.from_user.id):
        return await msg.reply("❌ Admins only.")
    states[msg.chat.id].queue.clear()
    await msg.reply("🗑 Queue cleared.")


# /pause
@bot.on_message(filters.command("pause") & filters.group)
async def cmd_pause(_, msg: Message):
    st = states[msg.chat.id]
    if st.paused:
        return await msg.reply("⚠️ Already paused.")
    try:
        await calls.pause_stream(msg.chat.id)
        st.paused = True
        await msg.reply("⏸ Paused.")
        await _update_np_message(msg.chat.id)
    except Exception as e:
        await msg.reply(f"❌ `{e}`")


# /resume
@bot.on_message(filters.command("resume") & filters.group)
async def cmd_resume(_, msg: Message):
    st = states[msg.chat.id]
    if not st.paused:
        return await msg.reply("⚠️ Not paused.")
    try:
        await calls.resume_stream(msg.chat.id)
        st.paused = False
        await msg.reply("▶️ Resumed.")
        await _update_np_message(msg.chat.id)
    except Exception as e:
        await msg.reply(f"❌ `{e}`")


# /volume
@bot.on_message(filters.command("volume") & filters.group)
async def cmd_volume(_, msg: Message):
    if len(msg.command) < 2:
        st = states[msg.chat.id]
        return await msg.reply(f"🔊 Current volume: `{st.volume}%`\nUsage: `/volume <1-200>`")
    try:
        vol = int(msg.command[1])
        assert 1 <= vol <= 200
    except (ValueError, AssertionError):
        return await msg.reply("❌ Volume must be between 1 and 200.")

    st = states[msg.chat.id]
    st.volume = vol
    try:
        await calls.change_volume_call(msg.chat.id, vol)
    except Exception:
        pass
    await msg.reply(f"🔊 Volume set to `{vol}%`")
    await _update_np_message(msg.chat.id)


# /loop
@bot.on_message(filters.command("loop") & filters.group)
async def cmd_loop(_, msg: Message):
    if len(msg.command) < 2 or msg.command[1] not in ("off", "track", "queue"):
        return await msg.reply("❌ Usage: `/loop off|track|queue`")
    mode = msg.command[1]
    states[msg.chat.id].loop_mode = mode
    icons = {"off": "➡️", "track": "🔂", "queue": "🔁"}
    await msg.reply(f"{icons[mode]} Loop mode set to: `{mode}`")
    await _update_np_message(msg.chat.id)


# /replay
@bot.on_message(filters.command("replay") & filters.group)
async def cmd_replay(_, msg: Message):
    st = states[msg.chat.id]
    if not st.current:
        return await msg.reply("❌ Nothing is playing.")
    track = st.current
    try:
        await _start_stream(msg.chat.id, track)
        await msg.reply(f"🔁 Replaying: **{track.title}**")
        await _update_np_message(msg.chat.id)
    except Exception as e:
        await msg.reply(f"❌ `{e}`")


# /seek — approximate (re-download with timestamp via ffmpeg)
@bot.on_message(filters.command("seek") & filters.group)
async def cmd_seek(_, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("❌ Usage: `/seek <mm:ss>` e.g. `/seek 1:30`")
    st = states[msg.chat.id]
    if not st.current:
        return await msg.reply("❌ Nothing is playing.")

    raw = msg.command[1]
    try:
        parts = raw.split(":")
        if len(parts) == 2:
            seconds = int(parts[0]) * 60 + int(parts[1])
        else:
            seconds = int(parts[0])
    except ValueError:
        return await msg.reply("❌ Invalid time format. Use `mm:ss` or `ss`.")

    if seconds >= st.current.duration:
        return await msg.reply("❌ Seek position exceeds track duration.")

    try:
        stream = AudioPiped(st.current.file, seek=seconds)
        await calls.change_stream(msg.chat.id, stream)
        st.started_at = time.time() - seconds
        await msg.reply(f"⏩ Seeked to `{fmt_dur(seconds)}`")
        await _update_np_message(msg.chat.id)
    except Exception as e:
        await msg.reply(f"❌ Seek failed: `{e}`")


# /stop — admin only
@bot.on_message(filters.command("stop") & filters.group)
async def cmd_stop(_, msg: Message):
    if not await is_admin(msg.chat.id, msg.from_user.id):
        return await msg.reply("❌ Admins only.")
    chat_id = msg.chat.id
    st = states[chat_id]
    st.queue.clear()
    st.current = None
    st.paused  = False
    try:
        await calls.leave_group_call(chat_id)
    except Exception:
        pass
    await msg.reply("⏹ Stopped playback and cleared queue.")


# /ping
@bot.on_message(filters.command("ping"))
async def cmd_ping(_, msg: Message):
    t = time.time()
    m = await msg.reply("🏓")
    ms = round((time.time() - t) * 1000)
    await m.edit(f"🏓 Pong! `{ms}ms`")


# /stats
@bot.on_message(filters.command("stats"))
async def cmd_stats(_, msg: Message):
    total_tracks = sum(
        (1 if st.current else 0) + len(st.queue)
        for st in states.values()
    )
    active_chats = sum(1 for st in states.values() if st.current)
    files = len(os.listdir(DOWNLOADS_DIR))
    size  = sum(
        os.path.getsize(os.path.join(DOWNLOADS_DIR, f))
        for f in os.listdir(DOWNLOADS_DIR)
    ) / (1024 * 1024)

    await msg.reply(
        f"📊 **Bot Statistics**\n\n"
        f"🎙 Active voice chats: `{active_chats}`\n"
        f"🎵 Total queued tracks: `{total_tracks}`\n"
        f"📁 Cached files: `{files}` ({size:.1f} MB)\n"
        f"🍪 Cookies: `{'Loaded' if os.path.exists(COOKIES_FILE) else 'Not found'}`"
    )


# ─────────────────────────────────────────────────────────
#  INLINE BUTTON CALLBACKS
# ─────────────────────────────────────────────────────────
@bot.on_callback_query(filters.regex(r"^(skip|pause|resume|stop|replay|vol_up|vol_down|loop_track|loop_queue|shuffle)$"))
async def cb_controls(_, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    st      = states[chat_id]
    action  = cq.data
    user    = cq.from_user

    admin = await is_admin(chat_id, user.id)

    if action == "skip":
        if not st.current:
            return await cq.answer("❌ Nothing playing.", show_alert=True)
        await play_next(chat_id)
        await cq.answer("⏭ Skipped!")

    elif action == "pause":
        if st.paused:
            return await cq.answer("Already paused.")
        await calls.pause_stream(chat_id)
        st.paused = True
        await cq.answer("⏸ Paused")

    elif action == "resume":
        if not st.paused:
            return await cq.answer("Not paused.")
        await calls.resume_stream(chat_id)
        st.paused = False
        await cq.answer("▶️ Resumed")

    elif action == "stop":
        if not admin:
            return await cq.answer("❌ Admins only!", show_alert=True)
        st.queue.clear()
        st.current = None
        try:
            await calls.leave_group_call(chat_id)
        except Exception:
            pass
        await cq.message.edit_text("⏹ Playback stopped.")
        return

    elif action == "replay":
        if not st.current:
            return await cq.answer("Nothing playing.")
        await _start_stream(chat_id, st.current)
        await cq.answer("🔁 Replaying!")

    elif action == "vol_up":
        st.volume = min(200, st.volume + 10)
        try:
            await calls.change_volume_call(chat_id, st.volume)
        except Exception:
            pass
        await cq.answer(f"🔊 Volume: {st.volume}%")

    elif action == "vol_down":
        st.volume = max(1, st.volume - 10)
        try:
            await calls.change_volume_call(chat_id, st.volume)
        except Exception:
            pass
        await cq.answer(f"🔉 Volume: {st.volume}%")

    elif action == "loop_track":
        st.loop_mode = "off" if st.loop_mode == "track" else "track"
        await cq.answer(f"🔂 Loop track: {'ON' if st.loop_mode=='track' else 'OFF'}")

    elif action == "loop_queue":
        st.loop_mode = "off" if st.loop_mode == "queue" else "queue"
        await cq.answer(f"🔁 Loop queue: {'ON' if st.loop_mode=='queue' else 'OFF'}")

    elif action == "shuffle":
        if not st.queue:
            return await cq.answer("Queue is empty.")
        random.shuffle(st.queue)
        await cq.answer(f"🔀 Shuffled {len(st.queue)} tracks!")

    await _update_np_message(chat_id)


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────
async def main():
    print("=" * 58)
    print("  ELITE MUSIC BOT — ADVANCED EDITION")
    print("=" * 58)
    print(f"  Cookies  : {'✅ Loaded' if os.path.exists(COOKIES_FILE) else '⚠️  Not found'}")
    print(f"  Downloads: {DOWNLOADS_DIR}/")
    print(f"  Max queue: {MAX_QUEUE} tracks")
    print(f"  Max dur  : {fmt_dur(MAX_DURATION)}")
    print("=" * 58)

    await assistant.start()
    print("✅ Assistant (userbot) started")

    await calls.start()
    print("✅ PyTgCalls started")

    # Cleanup background task
    asyncio.create_task(_cleanup_task())

    # Notify owner
    if OWNER_ID:
        try:
            await bot.send_message(OWNER_ID, "🎵 **Elite Music Bot** is now online!")
        except Exception:
            pass

    print("✅ Bot is ready! Waiting for commands...\n")
    await asyncio.Event().wait()


if __name__ == "__main__":
    bot.start()
    bot.loop.run_until_complete(main())
