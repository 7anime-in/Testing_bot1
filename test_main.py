import os
import re
import asyncio

# 🚨 FIX FOR RUNTIME ERROR: SET EVENT LOOP BEFORE PYROGRAM IMPORT
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from urllib.parse import quote
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse, Response, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from pyrogram import Client, filters
from pyrogram.errors import PeerIdInvalid, ChannelInvalid, RPCError, FloodWait

# ==================== ENVIRONMENT VARIABLES ====================
def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} set nahi hai (Render > Environment me add karein).")
    return value

API_ID = int(_require_env("API_ID"))
API_HASH = _require_env("API_HASH")
BOT_TOKEN = _require_env("BOT_TOKEN")
APP_URL = os.getenv("APP_URL", "https://sevenanime-http-bot.onrender.com")

CHANNEL_INPUT = os.getenv("CHANNEL_ID", "testing_c1")
CHANNEL_IDS = [ch.strip() for ch in CHANNEL_INPUT.split(",") if ch.strip()]

pyro_client = None
anime_database = {}

# Helper to verify if TG message is valid MP4 / Video
def is_video_message(message) -> bool:
    if not message or message.empty:
        return False
    if message.video:
        return True
    if message.document:
        mime = (message.document.mime_type or "").lower()
        fname = (message.document.file_name or "").lower()
        if mime.startswith("video/") or fname.endswith((".mp4", ".mkv", ".webm", ".avi", ".mov")):
            return True
    return False

# ==================== PARSING & DATABASE LOGIC ====================
def get_forward_title(message) -> str:
    chat = getattr(message, "forward_from_chat", None)
    if chat is not None and getattr(chat, "title", None):
        return chat.title
    return getattr(message, "forward_sender_name", None) or ""


def parse_anime_info(caption: str, forward_title: str = ""):
    text = caption or ""

    dub_type = "official"
    if re.search(r"\b(unofficial|fandub|fan[\s_\-]?dub)\b", text, re.IGNORECASE):
        dub_type = "unofficial"

    # Season / Episode (S01E05, Season 2, Episode 12, Ep-7 ...)
    season_match = re.search(r"(?<![A-Za-z])(?:Season|S)[\s\-_.:]*0*(\d+)(?!\d)", text, re.IGNORECASE)
    season = season_match.group(1) if season_match else "1"

    ep_match = re.search(r"(?<![A-Za-z])(?:Episode|Ep|E)[\s\-_.:]*0*(\d+)(?!\d)", text, re.IGNORECASE)
    bare_ep_token = None
    if not ep_match:
        clean_text = re.sub(r"\b(2160p|1080p|720p|480p|360p|x264|x265|hevc|20\d\d)\b", "", text, flags=re.IGNORECASE)
        ep_match = re.search(r"(?:^|[\s\-_\[])(\d{1,4})(?=$|[\s\-_\]]|\.mp4|\.mkv)", clean_text)
        if ep_match:
            bare_ep_token = ep_match.group(1)

    episode = int(ep_match.group(1)) if ep_match else 1

    # Anime name
    explicit_name = re.search(r"(?:Anime|Title|Name)\s*:\s*([^\n\r\t|]+)", text, re.IGNORECASE)

    if explicit_name:
        raw_title = explicit_name.group(1).strip()
    elif "solo leveling" in text.lower():
        raw_title = "Solo Leveling"
    elif forward_title and "solo leveling" in forward_title.lower():
        raw_title = "Solo Leveling"
    elif forward_title:
        raw_title = forward_title
    else:
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        raw_title = lines[0] if lines else "Testing Anime"

    if bare_ep_token and not explicit_name:
        raw_title = re.sub(rf"(?<!\d){re.escape(bare_ep_token)}(?!\d)", " ", raw_title)

    # Title me se episode/season/quality hatao, warna har episode ka alag anime ban jayega
    clean_title = re.sub(r"(?i)\.(mp4|mkv|webm|avi|mov)\b", " ", raw_title)
    clean_title = re.sub(r"(?i)\bS\d+\s*E\d+\b", " ", clean_title)
    clean_title = re.sub(r"(?i)(?<![A-Za-z])(season|episode|ep|s|e)[\s\-_.:]*\d+(?!\d)", " ", clean_title)
    clean_title = re.sub(r"(?i)\bin\s+(hindi|english|urdu|tamil|telugu)\b", " ", clean_title)
    clean_title = re.sub(
        r"(?i)\b(hindi|dubbed|dub|sub|official|unofficial|fandub|2160p|1080p|720p|480p|360p|4k|fhd|hd|hevc|x264|x265|episode|season|language|quality|main channel)\b",
        " ",
        clean_title,
    )
    clean_title = re.sub(r"[^\w\s]", " ", clean_title)
    clean_title = re.sub(r"\s+", " ", clean_title).strip().title()

    if not clean_title or len(clean_title) < 2:
        clean_title = "Testing Anime"

    return clean_title, str(int(season)), episode, dub_type


def add_to_database(chat_id: str, msg_id: int, caption: str, forward_title: str):
    anime_name, season_num, ep_num, dub_type = parse_anime_info(caption, forward_title)
    slug_key = anime_name.lower().replace(" ", "_")

    if slug_key not in anime_database:
        anime_database[slug_key] = {"title": anime_name, "seasons": {}}

    anime_database[slug_key]["title"] = anime_name

    if season_num not in anime_database[slug_key]["seasons"]:
        anime_database[slug_key]["seasons"][season_num] = []

    ep_list = anime_database[slug_key]["seasons"][season_num]
    
    existing_ep = next((item for item in ep_list if item["ep"] == ep_num and item.get("type", "official") == dub_type), None)

    formatted_chat_id = chat_id if chat_id.startswith("-") or chat_id.startswith("@") or chat_id.isdigit() else f"@{chat_id}"

    if existing_ep:
        existing_ep["chat_id"] = str(formatted_chat_id)
        existing_ep["msg_id"] = msg_id
    else:
        ep_list.append({
            "ep": ep_num,
            "chat_id": str(formatted_chat_id),
            "msg_id": msg_id,
            "type": dub_type
        })
        ep_list.sort(key=lambda x: x["ep"])


async def auto_scan_channels():
    print("🔍 Scanning Telegram Channels...")

    for ch_id in CHANNEL_IDS:
        if not ch_id:
            continue
        try:
            target_chat = int(ch_id) if (ch_id.startswith("-") or ch_id.isdigit()) else (ch_id if ch_id.startswith("@") else f"@{ch_id}")
            
            chunk_size = 100
            current_id = 1
            empty_count = 0

            while empty_count < 5:
                msg_ids = list(range(current_id, current_id + chunk_size))
                try:
                    messages = await pyro_client.get_messages(target_chat, msg_ids)
                    has_media_in_chunk = False

                    if messages:
                        for message in messages:
                            if is_video_message(message):
                                has_media_in_chunk = True
                                caption = message.caption or getattr(message.video or message.document, "file_name", "") or ""
                                forward_title = get_forward_title(message)
                                add_to_database(str(target_chat), message.id, caption, forward_title)

                    # Jab tak channel me koi bhi message mil raha hai scan chalta rahega
                    has_any_message = any(m and not m.empty for m in (messages or []))
                    if has_any_message:
                        empty_count = 0
                    else:
                        empty_count += 1

                    current_id += chunk_size
                    await asyncio.sleep(0.1)

                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except (PeerIdInvalid, ChannelInvalid) as e:
                    print(f"⚠️ Channel '{target_chat}' access nahi ho raha (bot channel me admin hai?): {e}")
                    break
                except Exception as e:
                    print(f"Batch fetch info at ID {current_id}: {e}")
                    current_id += chunk_size
                    empty_count += 1

            print(f"✅ Channel '{target_chat}' scanned completely!")
        except Exception as e:
            print(f"⚠️ Error scanning channel {ch_id}: {e}")

# ==================== LIFECYCLE & BOT HANDLERS ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global pyro_client
    print("Starting Pyrogram Engine...")

    pyro_client = Client(
        "sevenanime_bot_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
    )

    @pyro_client.on_message(filters.command("start"))
    async def start_cmd(client, message):
        await message.reply_text(
            "👋 **Namaste! Welcome to SevenAnime Engine Bot**\n\n"
            "Mai aapki Telegram channel ki anime videos ko Web Player aur Website se connect karta hu.\n\n"
            "🛠 **Commands:**\n"
            "• `/start` - Check bot status\n"
            "• `/stats` - Total indexed anime and episode count",
            quote=True,
        )

    @pyro_client.on_message(filters.command("stats"))
    async def stats_cmd(client, message):
        total_anime = len(anime_database)
        total_eps = sum(
            len(ep_list)
            for anime in anime_database.values()
            for ep_list in anime.get("seasons", {}).values()
        )
        await message.reply_text(
            f"📊 **Database Statistics:**\n\n"
            f"⛩️ **Total Anime:** `{total_anime}`\n"
            f"🎬 **Total Episodes:** `{total_eps}`",
            quote=True,
        )

    @pyro_client.on_message((filters.video | filters.document) & ~filters.command(["start", "stats"]))
    async def auto_link_gen(client, message):
        if not is_video_message(message):
            return

        chat = message.chat
        chat_identifier = f"@{chat.username}" if chat.username else str(chat.id)
        msg_id = message.id
        base_url = APP_URL.rstrip("/")

        caption = message.caption or getattr(message.video or message.document, "file_name", "") or ""
        forward_title = get_forward_title(message)

        add_to_database(chat_identifier, msg_id, caption, forward_title)

        anime_name, season_num, ep_num, dub_type = parse_anime_info(caption, forward_title)
        clean_chat = chat_identifier.replace("@", "")
        card_slug = anime_name.lower().replace(" ", "_")
        stream_url = f"{base_url}/stream/{clean_chat}/{msg_id}.mp4"
        download_url = f"{base_url}/download/{clean_chat}/{msg_id}"

        await message.reply_text(
            f"🎬 **Added to Database!**\n\n"
            f"⛩️ **Anime:** `{anime_name}`\n"
            f"🔖 **Card Slug (index.html data-slug):** `{card_slug}`\n"
            f"🎙️ **Type:** `{dub_type.upper()}`\n"
            f"📦 **Season:** `{season_num}` | **Episode:** `{ep_num}`\n"
            f"📺 **Stream:** `{stream_url}`\n"
            f"📥 **Download:** `{download_url}`",
            quote=True,
        )

    await pyro_client.start()
    asyncio.create_task(auto_scan_channels())
    print("SevenAnime Engine Live!")
    yield
    await pyro_client.stop()


app = FastAPI(title="SevenAnime Engine", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "Content-Type", "Content-Disposition"],
)

# ==================== FASTAPI ENDPOINTS ====================

@app.api_route("/", methods=["GET", "HEAD"])
def home():
    return {"status": "SevenAnime Engine Active 🚀"}

# 🎬 WEB PLAYER ENDPOINT
@app.get("/player", response_class=HTMLResponse)
def get_web_player():
    if os.path.exists("videoplayer.html"):
        with open("videoplayer.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h2>videoplayer.html file nahi mili! Directory me file check karein.</h2>"

def _serve_html(filename: str):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse(f"<h2>{filename} nahi mili!</h2>", status_code=404)

@app.get("/index.html", response_class=HTMLResponse)
def get_index_page():
    return _serve_html("index.html")

@app.get("/videoplayer.html", response_class=HTMLResponse)
def get_videoplayer_page():
    return _serve_html("videoplayer.html")

@app.get("/api/all-anime")
def get_all_anime():
    return anime_database

@app.get("/api/episodes/{anime_slug}")
def get_anime_episodes(anime_slug: str):
    slug = anime_slug.lower().replace("-", "_")
    if slug in anime_database:
        return anime_database[slug]

    for key in anime_database:
        if slug in key or key in slug:
            return anime_database[key]

    return {"title": slug.replace("_", " ").title(), "seasons": {"1": []}}


async def get_media_response(
    chat_id: str,
    message_id: str,
    request: Request,
    range_header: str,
    is_download: bool = False,
):
    if request.method == "OPTIONS":
        return Response(status_code=200, headers={"Access-Control-Allow-Origin": "*"})

    msg_id_clean = int(str(message_id).replace(".mp4", "").replace(".mkv", ""))

    if not pyro_client:
        if is_download:
            raise HTTPException(status_code=503, detail="Telegram engine offline hai.")
        return Response(content=b"", media_type="video/mp4", status_code=503)

    try:
        target_id = int(chat_id) if (chat_id.startswith("-") or chat_id.isdigit()) else (chat_id if chat_id.startswith("@") else f"@{chat_id}")
        msg = await pyro_client.get_messages(target_id, msg_id_clean)
    except Exception as e:
        if is_download:
            raise HTTPException(status_code=404, detail=f"Video message nahi mila: {str(e)}")
        return Response(content=b"", media_type="video/mp4", status_code=404)

    if not is_video_message(msg):
        if is_download:
            raise HTTPException(status_code=400, detail="Is message me koi valid video nahi hai")
        return Response(content=b"", media_type="video/mp4", status_code=400)

    media = msg.video or msg.document
    file_size = media.file_size
    file_name = getattr(media, "file_name", f"{msg_id_clean}.mp4") or f"{msg_id_clean}.mp4"

    from_bytes = 0
    until_bytes = file_size - 1

    if range_header:
        range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if range_match:
            start = range_match.group(1)
            end = range_match.group(2)
            from_bytes = int(start) if start else 0
            until_bytes = min(int(end), file_size - 1) if end else file_size - 1

    if from_bytes >= file_size or from_bytes > until_bytes:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

    chunk_length = until_bytes - from_bytes + 1

    if is_download:
        mime_type = "application/octet-stream"
        disposition = f"attachment; filename*=UTF-8''{quote(file_name)}"
    elif file_name.lower().endswith(".mkv"):
        mime_type = "video/x-matroska"
        disposition = f"inline; filename=\"{file_name}\""
    else:
        mime_type = "video/mp4"
        disposition = f"inline; filename=\"{file_name}\""

    headers = {
        "Content-Type": mime_type,
        "Content-Disposition": disposition,
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_length),
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges, Content-Type, Content-Disposition",
        "Cache-Control": "no-cache",
    }

    if range_header:
        headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"

    if request.method == "HEAD":
        return Response(status_code=206 if range_header else 200, headers=headers)

    chunk_size = 1024 * 1024
    start_chunk = from_bytes // chunk_size
    skip_bytes = from_bytes % chunk_size

    async def media_streamer():
        bytes_sent = 0
        current_skipped = 0
        try:
            async for chunk in pyro_client.stream_media(msg, offset=start_chunk):
                if current_skipped < skip_bytes:
                    if current_skipped + len(chunk) <= skip_bytes:
                        current_skipped += len(chunk)
                        continue
                    else:
                        needed = skip_bytes - current_skipped
                        chunk = chunk[needed:]
                        current_skipped = skip_bytes

                remaining = chunk_length - bytes_sent
                if len(chunk) >= remaining:
                    yield chunk[:remaining]
                    break

                yield chunk
                bytes_sent += len(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Stream error: {e}")

    status_code = 206 if range_header else 200
    return StreamingResponse(media_streamer(), status_code=status_code, headers=headers)


@app.api_route("/stream/{chat_id}/{message_id}", methods=["GET", "HEAD", "OPTIONS"])
@app.api_route("/stream/{chat_id}/{message_id}.mp4", methods=["GET", "HEAD", "OPTIONS"])
async def stream_video(chat_id: str, message_id: str, request: Request, range: str = Header(None)):
    return await get_media_response(chat_id, message_id, request, range, is_download=False)

@app.api_route("/download/{chat_id}/{message_id}", methods=["GET", "HEAD", "OPTIONS"])
@app.api_route("/download/{chat_id}/{message_id}.mp4", methods=["GET", "HEAD", "OPTIONS"])
async def download_video(chat_id: str, message_id: str, request: Request, range: str = Header(None)):
    return await get_media_response(chat_id, message_id, request, range, is_download=True)

    
