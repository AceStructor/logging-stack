from pydoc import html
import sys
import logging
import structlog
import json
import os
import asyncio
import time
import aiohttp
from typing import Optional

from nio import AsyncClient, RoomMessageText, LoginResponse, RoomSendResponse
from config import MATRIX_HOMESERVER, MATRIX_USER, MATRIX_PASSWORD, TOKEN_FILE, MATRIX_ROOM_ID, LOKI_URL

# ======================
# LOGGING
# ======================
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=logging.DEBUG,
)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
)

log = structlog.get_logger(service="matrix-alert-bot")

# ======================
# ENV
# ======================
POLL_INTERVAL = 2

# ======================
# STATE
# ======================
LOGLEVELS = ["debug", "info", "warning", "error"]
current_level = "warning"
last_timestamp_ns: Optional[int] = None

LEVEL_ORDER = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
}

# ======================
# LOKI QUERY BUILDER
# ======================
def build_logql(level: str) -> str:
    allowed = [
        lvl for lvl, idx in LEVEL_ORDER.items()
        if idx >= LEVEL_ORDER[level]
    ]
    regex = "|".join(allowed)
    query = f'{{container=~".+", level=~"{regex}"}} | json'
    log.debug("Built LogQL query", level=level, query=query)
    return query

# ======================
# MATRIX BOT
# ======================
def ensure_token_dir():
    token_dir = os.path.dirname(TOKEN_FILE)
    if token_dir:
        os.makedirs(token_dir, exist_ok=True)

async def get_matrix_client() -> AsyncClient:
    client = AsyncClient(MATRIX_HOMESERVER, MATRIX_USER)

    # Sicherstellen, dass das Verzeichnis existiert
    ensure_token_dir()

    # 1) Session laden (falls vorhanden)
    if os.path.isfile(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            session = json.load(f)

        client.access_token = session.get("access_token")
        client.user_id = session.get("user_id")
        client.device_id = session.get("device_id")
        client.sync_token = session.get("sync_token")

        if client.access_token:
            log.debug("Matrix-Session geladen.")

    # 2) Login nur wenn nötig
    if not client.access_token:
        log.info("Matrix Login (First Run)...")
        try:
            resp = await client.login(MATRIX_PASSWORD)
        except Exception:
            log.error("Exception during Matrix login", exc_info=True)
            raise

        if not isinstance(resp, LoginResponse):
            log.error("Matrix login failed", response=resp)
            raise RuntimeError(f"Matrix login failed: {resp}")

        # Atomisch schreiben
        tmp_file = f"{TOKEN_FILE}.tmp"
        try:
            with open(tmp_file, "w") as f:
                json.dump(
                    {
                        "access_token": resp.access_token,
                        "user_id": resp.user_id,
                        "device_id": resp.device_id,
                    },
                    f,
                )
            os.replace(tmp_file, TOKEN_FILE)
            log.info("Matrix-Session gespeichert.")
        except Exception:
            log.warning("Failed to save Matrix session", exc_info=True)
            # Fail fast - try to remove tmp file if present
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                log.debug("Failed to remove tmp session file", tmp=tmp_file)

    return client

def format_log_for_matrix(log_json: str) -> dict:
    """
    Formatiert ein JSON-Log in eine lesbare HTML-Version für Matrix-Nachrichten.
    
    Args:
        log_json (str): Das Log als JSON-String.
        
    Returns:
        dict: Dictionary mit msgtype, body und formatted_body.
    """
    try:
        log = json.loads(log_json)
    except json.JSONDecodeError:
        return {
            "msgtype": "m.text",
            "body": f"Ungültiges JSON: {log_json}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<strong>Ungültiges JSON</strong>: {html.escape(log_json)}"
        }

    # Basisinfos
    service = log.get("service", "unknown_service")
    event = log.get("event", "unknown_event")
    level = log.get("level", "info").lower()
    ts = log.get("ts", "")

    # Farben nach Level
    colors = {
        "error": "red",
        "warning": "orange",
        "info": "blue",
        "debug": "gray"
    }
    color = colors.get(level, "black")

    # Alle weiteren Keys außer den Standard-Keys
    extra_keys = {k: v for k, v in log.items() if k not in {"service", "event", "level", "ts"}}
    
    # Plain text body (Fallback)
    extras_text = ", ".join(f"{k}={v}" for k, v in extra_keys.items()) if extra_keys else ""
    body = f"[{ts}] [{level.upper()}] {service}: {event}" + (f" ({extras_text})" if extras_text else "")

    # HTML formatted_body
    extras_html = "".join(f"<br><code>{html.escape(k)} = {html.escape(str(v))}</code>" for k, v in extra_keys.items())
    formatted_body = (
        f"<span style='color:{color};'><strong>[{html.escape(ts)}] [{level.upper()}]</strong></span> "
        f"<strong>{html.escape(service)}</strong>: <em>{html.escape(event)}</em>"
        f"{extras_html}"
    )

    return {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_body
    }

async def run_bot():
    global current_level, last_timestamp_ns

    client = await get_matrix_client()

    session = aiohttp.ClientSession()
    resp = await client.sync(timeout=3000)
    
    try:
        if client.access_token:
            resp = await client.room_send(
                room_id=MATRIX_ROOM_ID,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": "✅ matrix-alert-bot started"},
            )

            # Only log error if send failed
            if not isinstance(resp, RoomSendResponse) or getattr(getattr(resp, "transport_response", None), "status", 200) != 200:
                log.error(
                    "Matrix send failed",
                    status_code=getattr(getattr(resp, "transport_response", None), "status", None),
                    message=getattr(resp, "message", None),
                    raw=resp,
                )
            else:
                log.info("Startup announcement sent")

    except Exception:
        log.warning("Failed to send startup announcement to Matrix room", exc_info=True)

    # ----------------------
    # Matrix command handler
    # ----------------------
    async def on_message(room, event: RoomMessageText):
        global current_level

        if room.room_id != MATRIX_ROOM_ID:
            return

        body = event.body.strip()
        log.debug("Received message", room_id=room.room_id, sender=event.sender, body=body)

        if not body.startswith("!"):
            return

        if body.startswith("!loglevel"):
            try:
                _, level = body.split()
                level = level.lower()
            except ValueError:
                log.warning("Invalid !loglevel command format", body=body)
                return

            if level not in LOGLEVELS:
                try:
                    await client.room_send(
                        MATRIX_ROOM_ID,
                        "m.room.message",
                        {"msgtype": "m.text", "body": "❌ Unknown log level"},
                    )
                except Exception:
                    log.warning("Failed to send unknown level reply", exc_info=True)
                return

            current_level = level
            log.debug("Log level changed via chat command", level=current_level, sender=event.sender)
            try:
                await client.room_send(
                    MATRIX_ROOM_ID,
                    "m.room.message",
                    {
                        "msgtype": "m.text",
                        "body": f"✅ Streaming logs with level ≥ **{current_level.upper()}**",
                    },
                )
            except Exception:
                log.warning("Failed to send loglevel confirmation", exc_info=True)

    client.add_event_callback(on_message, RoomMessageText)

    # ----------------------
    # Loki polling task
    # ----------------------
    async def poll_loki():
        global last_timestamp_ns

        while True:
            try:
                query = build_logql(current_level)
                log.debug("Polling Loki", query=query, last_timestamp_ns=last_timestamp_ns)
                now_ns = int(time.time() * 1e9)

                params = {
                    "query": query,
                    "limit": 50,
                    "direction": "forward",
                }

                if last_timestamp_ns:
                    params["start"] = last_timestamp_ns + 1

                async with session.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params) as resp:
                    # Read the body text up front so we can log useful debug info for non-200 responses
                    text = await resp.text()
                    if resp.status != 200:
                        # Include response body and the query to make 400s (Bad Request) diagnosable
                        if resp.status == 400:
                            log.error("Loki returned 400 Bad Request (check LogQL)", status=resp.status, text=text, query=query)
                            # Back off longer for bad requests to avoid spamming Loki while the query/filters are being fixed
                            await asyncio.sleep(POLL_INTERVAL * 10)
                        else:
                            log.warning("Loki returned non-200 status", status=resp.status, text=text, query=query)
                            await asyncio.sleep(POLL_INTERVAL)
                        continue
                    try:
                        data = json.loads(text)
                    except Exception:
                        log.error("Failed to decode Loki response as JSON", text=text)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                try:
                    streams = data["data"]["result"]
                    if len(streams) == 0:
                        log.debug("No new logs from Loki")
                    else:
                        log.debug("Received logs from Loki", stream_count=len(streams))
                except KeyError:
                    log.error("Unexpected Loki response structure", data=data)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                for stream in streams:
                    for ts, line in stream["values"]:
                        log.debug("Forwarding log line to Matrix", timestamp=ts, line=line, last_timestamp_ns=last_timestamp_ns)
                        last_timestamp_ns = max(last_timestamp_ns or 0, int(ts))
                        content = format_log_for_matrix(line)
                        try:
                            await client.room_send(
                                room_id=MATRIX_ROOM_ID,
                                message_type="m.room.message",
                                content=content,
                            )
                        except Exception:
                            log.warning("Failed to send message to Matrix room", exc_info=True)

            except aiohttp.ClientError as e:
                log.warning("Network error when polling Loki", exc_info=True)
            except Exception:
                log.error("Unexpected error in poll_loki", exc_info=True)
            finally:
                await asyncio.sleep(POLL_INTERVAL)

    # ----------------------
    # Run both tasks
    # ----------------------
    try:
        await asyncio.gather(
            client.sync_forever(timeout=30000),
            poll_loki(),
        )
    except asyncio.CancelledError:
        log.info("Tasks cancelled, shutting down")
        raise
    except Exception:
        log.error("Unhandled exception in run_bot", exc_info=True)
        raise
    finally:
        log.info("Shutting down client and HTTP session")
        try:
            await client.close()
        except Exception:
            log.warning("Failed to close Matrix client", exc_info=True)
        try:
            await session.close()
        except TypeError:
            session.close()
        except Exception:
            log.warning("Failed to close HTTP session", exc_info=True)

# ======================
# ENTRYPOINT
# ======================
if __name__ == "__main__":
    try:
        log.info("Starting matrix-alert-bot")
        asyncio.run(run_bot())
    except Exception:
        log.error("Unhandled exception in main", exc_info=True)
        sys.exit(1)
