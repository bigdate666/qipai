# -*- coding: utf-8 -*-
"""
棋牌对战 云端版 —— FastAPI + WebSocket(用于 Hugging Face Spaces 等云平台)
支持: 欢乐斗牛(douniu) / 炸金花(zjh)。游戏逻辑在 gamecore.py(需一并上传)。
页面: GET /    通信: WebSocket /ws
"""
import asyncio
import json
import os
import random
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

import gamecore as gc

app = FastAPI()

ROOMS = {}


def make_room_code():
    while True:
        code = "".join(random.choices("0123456789", k=4))
        if code not in ROOMS:
            return code


INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/")
def home():
    return HTMLResponse(INDEX_HTML)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    player = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")

            if t == "create":
                if player:
                    continue
                game = msg.get("game") if msg.get("game") in gc.GAMES else "douniu"
                player = gc.Player(ws, msg.get("name", ""))
                code = make_room_code()
                room_cls = gc.ZhajinhuaRoom if game == "zjh" else gc.DouniuRoom
                room = room_cls(code)
                try:
                    room.total_rounds = max(0, min(100, int(msg.get("rounds") or 0)))
                except (TypeError, ValueError):
                    room.total_rounds = 0
                ROOMS[code] = room
                room.players.append(player)
                player.room = room
                room.broadcast()

            elif t == "join":
                if player:
                    continue
                code = str(msg.get("room", "")).strip()
                room = ROOMS.get(code)
                if room is None:
                    await ws.send_text(json.dumps({"type": "error", "msg": "房间不存在, 请检查房间号"}, ensure_ascii=False))
                    continue
                if len(room.players) >= gc.MAX_PLAYERS:
                    await ws.send_text(json.dumps({"type": "error", "msg": "房间已满(最多5人)"}, ensure_ascii=False))
                    continue
                player = gc.Player(ws, msg.get("name", ""))
                room.players.append(player)
                player.room = room
                room.send(player, {"type": "joined"})
                room.broadcast()

            elif player is None or player.room is None:
                await ws.send_text(json.dumps({"type": "error", "msg": "请先创建或加入房间"}, ensure_ascii=False))

            elif t == "ready":
                player.room.on_ready(player)
            elif t == "grab":
                if isinstance(player.room, gc.DouniuRoom):
                    player.room.on_grab(player, msg.get("value", 0))
            elif t == "action":
                player.room.on_action(player, msg.get("action", ""), msg.get("target"))
            elif t == "next":
                player.room.on_next(player)
            elif t == "restart":
                player.room.on_restart(player)
            elif t == "relief":
                player.room.on_relief(player)
            elif t == "sitout":
                player.room.on_leave_round(player)
            elif t == "chat":
                player.room.on_chat(player, msg.get("text", ""))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if player and player.room:
            room = player.room
            room.remove_player(player)
            player.room = None
            if not room.players:
                ROOMS.pop(room.code, None)


if __name__ == "__main__":
    import uvicorn
    # Hugging Face Spaces 免费档: Docker SDK 已收费, 改用 Gradio SDK 外壳挂载本服务
    try:
        import gradio as gr
        with gr.Blocks() as demo:
            gr.Markdown("🎴 棋牌服务器运行中, 请访问根路径 / 进入游戏")
        mounted = gr.mount_gradio_app(app, demo, path="/hf_admin")
    except Exception:
        mounted = app
    uvicorn.run(mounted, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
