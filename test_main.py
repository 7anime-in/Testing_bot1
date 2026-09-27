import os
import re
import asyncio
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pyrogram import Client

# ==================== NEW TEST CREDENTIALS ====================
API_ID = int(os.getenv("API_ID", "31169133"))
API_HASH = os.getenv("API_HASH", "b836f4b836df4cf83c2d475a5ad3b285")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8891627372:AAF8MIvp06YxSmZwRRyGLt8e0rNIJV82q-U")

pyro_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pyro_client
    print("🚀 Starting Clean Test Streaming Engine...")
    pyro_client = Client("test_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
    await pyro_client.start()
    print("✅ Pyrogram Client Successfully Connected!")
    yield
    await pyro_client.stop()

app = FastAPI(title="7Anime Clean Test Engine", lifespan=lifespan)

# Enable Full CORS Support for Browser Playback
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "Content-Type"],
)

@app.get("/")
def test_root():
    return {"status": "Test Backend Active 🟢", "channel": "@testing_c1"}

@app.api_route("/stream/{chat_id}/{message_id}", methods=["GET", "HEAD", "OPTIONS"])
async def test_stream(chat_id: str, message_id: str, request: Request, range: str = Header(None)):
    if request.method == "OPTIONS":
        return Response(status_code=200, headers={"Access-Control-Allow-Origin": "*"})

    # Clean message ID
    msg_id = int(str(message_id).replace(".mp4", "").replace(".mkv", ""))
    
    # Handle Chat ID Format
    target_chat = int(chat_id) if (chat_id.startswith("-") or chat_id.isdigit()) else (chat_id if chat_id.startswith("@") else f"@{chat_id}")

    try:
        msg = await pyro_client.get_messages(target_chat, msg_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Telegram Error: {str(e)}")

    if not msg or msg.empty:
        raise HTTPException(status_code=404, detail="Message nahi mila ya bot ke paas Channel Permission nahi hai.")

    # Detect Video or Document Video
    media = msg.video or msg.document
    if not media:
        raise HTTPException(status_code=400, detail="Is message me koi Video file nahi mili!")

    file_size = media.file_size
    from_bytes = 0
    until_bytes = file_size - 1

    # HTTP Range Header Processing for Video Seek
    if range:
        range_match = re.search(r"bytes=(\d+)-(\d*)", range)
        if range_match:
            start = range_match.group(1)
            end = range_match.group(2)
            from_bytes = int(start) if start else 0
            until_bytes = int(end) if end else file_size - 1

    chunk_length = until_bytes - from_bytes + 1

    # Set Correct MIME Type for Native Player
    file_name = getattr(media, "file_name", "video.mp4") or "video.mp4"
    if file_name.lower().endswith(".mkv"):
        mime_type = "video/x-matroska"
    else:
        mime_type = "video/mp4"

    headers = {
        "Content-Type": mime_type,
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Length": str(chunk_length),
        "Access-Control-Allow-Origin": "*",
    }

    if request.method == "HEAD":
        return Response(status_code=206 if range else 200, headers=headers)

    # Stream Chunks directly from Telegram
    async def media_streamer():
        try:
            async for chunk in pyro_client.stream_media(msg, offset=from_bytes):
                yield chunk
        except Exception as e:
            print(f"Streaming Exception: {e}")

    return StreamingResponse(media_streamer(), status_code=206 if range else 200, headers=headers)
    
