# server.py
import asyncio, json, os, uuid, base64
from datetime import datetime
from typing import Dict, Any, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="AirDrop (FastAPI) - Presence + Fast WS transfer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static client
app.mount("/static", StaticFiles(directory="static"), name="static")

DEVICES: Dict[str, Dict[str, Any]] = {}   # device_id -> {meta..., ws, last_seen, pending: [bytes,...]}
LOCK = asyncio.Lock()

@app.get("/")
async def index():
    return HTMLResponse(open("static/index.html", encoding="utf-8").read())

@app.get("/devices")
async def list_devices():
    """Return current devices (public metadata only)"""
    async with LOCK:
        return {
            "devices": [
                {
                    "device_id": d,
                    "name": DEVICES[d]["meta"]["device_name"],
                    "os": DEVICES[d]["meta"].get("os"),
                    "model": DEVICES[d]["meta"].get("model"),
                    "avatar": DEVICES[d]["meta"].get("avatar") or "",
                    "observed_ip": DEVICES[d]["observed_ip"],
                    "online": DEVICES[d]["online"]
                } for d in DEVICES
            ]
        }

@app.get("/pubmeta/{device_id}")
async def get_pubmeta(device_id: str):
    async with LOCK:
        if device_id not in DEVICES:
            raise HTTPException(404)
        return {"meta": DEVICES[device_id]["meta"], "observed_ip": DEVICES[device_id]["observed_ip"], "online": DEVICES[device_id]["online"]}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    device_id = None
    try:
        # First message must be registration JSON: {action:'register', device_id, meta:{device_name, os, model, avatar (base64 small image), public_key_jwk}}
        raw = await ws.receive_text()
        obj = json.loads(raw)
        if obj.get("action") != "register":
            await ws.send_text(json.dumps({"error":"first message must be register"}))
            await ws.close()
            return

        device_id = obj["device_id"]
        meta = obj.get("meta", {})
        observed_ip = ws.client.host if ws.client else "unknown"
        async with LOCK:
            DEVICES[device_id] = {"meta": meta, "ws": ws, "last_seen": datetime.utcnow().isoformat(), "observed_ip": observed_ip, "online": True, "pending": []}
        # notify client of successful registration + server-assigned timestamp
        await ws.send_text(json.dumps({"action":"registered", "device_id": device_id, "observed_ip": observed_ip, "server_time": datetime.utcnow().isoformat()}))
        # broadcast presence to other connected devices
        await notify_presence()

        # deliver any pending messages queued while offline
        async with LOCK:
            pending = DEVICES[device_id]["pending"][:]
            DEVICES[device_id]["pending"].clear()
        for p in pending:
            await ws.send_bytes(p)

        # main loop: accept text control messages or bytes for fast forward
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" in msg:
                data = json.loads(msg["text"])
                # accept control commands: 'list', 'ping', 'offer', 'accept' etc
                if data.get("action") == "list":
                    async with LOCK:
                        devices = [ {"device_id":d, "meta":DEVICES[d]["meta"], "observed_ip":DEVICES[d]["observed_ip"], "online": DEVICES[d]["online"]} for d in DEVICES ]
                    await ws.send_text(json.dumps({"action":"list", "devices": devices}))
                elif data.get("action") == "ping":
                    await ws.send_text(json.dumps({"action":"pong", "ts": datetime.utcnow().isoformat()}))
                else:
                    # other control messages (forward to recipient): contains fields {action:'control', to:target_id, payload:{...}}
                    if data.get("to") and data.get("payload"):
                        to = data["to"]
                        async with LOCK:
                            target = DEVICES.get(to)
                        if target and target["online"]:
                            try:
                                await target["ws"].send_text(json.dumps({"from": device_id, "control": data["payload"]}))
                                await ws.send_text(json.dumps({"action":"forwarded", "to": to}))
                            except Exception:
                                # queue
                                async with LOCK:
                                    DEVICES[to]["pending"].append(json.dumps({"from": device_id, "control": data["payload"]}).encode("utf-8"))
                                await ws.send_text(json.dumps({"action":"queued", "to": to}))
                        else:
                            await ws.send_text(json.dumps({"action":"not_online", "to": to}))
                continue

            if "bytes" in msg:
                # binary frames are opaque envelope: server prepends envelope JSON with boundary then forwards to recipient.
                # Envelope format: JSON header bytes + b"\n--AIRDROP-BOUNDARY--\n" + raw_encrypted_blob
                # Server will forward as-is to recipient if online, else queue.
                payload = msg["bytes"]
                # parse envelope header quickly to find "to" property (header terminated by sentinel)
                sentinel = b"\n--AIRDROP-BOUNDARY--\n"
                try:
                    idx = payload.index(sentinel)
                    header = payload[:idx]
                    envelope = json.loads(header.decode("utf-8"))
                    to = envelope.get("to")
                except Exception:
                    # cannot parse -> ignore or put in broadcast queue
                    await ws.send_text(json.dumps({"error":"malformed envelope"}))
                    continue

                async with LOCK:
                    recipient = DEVICES.get(to)
                    if recipient and recipient["online"]:
                        try:
                            await recipient["ws"].send_bytes(payload)
                            await ws.send_text(json.dumps({"action":"sent", "to": to, "transfer_id": envelope.get("transfer_id")}))
                        except Exception:
                            # queue if send fails
                            DEVICES[to]["pending"].append(payload)
                            await ws.send_text(json.dumps({"action":"queued", "to": to}))
                    else:
                        # queue
                        if to in DEVICES:
                            DEVICES[to]["pending"].append(payload)
                        else:
                            # unknown recipient: drop or return error
                            await ws.send_text(json.dumps({"action":"unknown_recipient", "to": to}))
            # loop continues

    except WebSocketDisconnect:
        pass
    except Exception as e:
        # unexpected
        try:
            await ws.send_text(json.dumps({"error":"server_error", "detail": str(e)}))
        except:
            pass
    finally:
        # cleanup: mark offline and broadcast
        if device_id:
            async with LOCK:
                if device_id in DEVICES:
                    DEVICES[device_id]["online"] = False
                    DEVICES[device_id]["last_seen"] = datetime.utcnow().isoformat()
                    DEVICES[device_id]["ws"] = None
            await notify_presence()

async def notify_presence():
    """Broadcast current device list to all online clients (presence updates)."""
    async with LOCK:
        snapshot = [
            {"device_id": d,
             "device_name": DEVICES[d]["meta"].get("device_name"),
             "os": DEVICES[d]["meta"].get("os"),
             "model": DEVICES[d]["meta"].get("model"),
             "avatar": DEVICES[d]["meta"].get("avatar") or "",
             "observed_ip": DEVICES[d]["observed_ip"],
             "online": DEVICES[d]["online"]}
            for d in DEVICES
        ]
        for d in DEVICES:
            if DEVICES[d].get("online") and DEVICES[d].get("ws"):
                try:
                    await DEVICES[d]["ws"].send_text(json.dumps({"action":"presence", "devices": snapshot}))
                except Exception:
                    # ignore send errors
                    pass

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
