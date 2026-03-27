from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional
from uuid import uuid4

import cv2
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.webrtc import (
    WebRtcDependencyError,
    WebRtcSession,
    webrtc_available,
    webrtc_error_message,
)
from config import load_app_config
from ipc.control_channel import CommandSender
from ipc.shared_ring import SharedFrameRing
from ipc.shared_text_channel import SharedTextChannel

app = FastAPI(title="RealSense IPC WebRTC Stream", version="0.2.0")
LOGGER = logging.getLogger(__name__)

_app_config = load_app_config()
_vcfg = _app_config.video
_icfg = _app_config.ipc
_wcfg = _app_config.webrtc
_resolved_wcfg = _wcfg.resolved()
_ring: Optional[SharedFrameRing] = None
_command_channel: Optional[SharedTextChannel] = None
_command_sender: Optional[CommandSender] = None


class CommandResponse(BaseModel):
    ok: bool
    command: str
    request_id: str
    message: str


class ApiStatusResponse(BaseModel):
    ok: bool
    ring_connected: bool
    command_connected: bool
    webrtc_available: bool
    message: str


def _attach_ring_if_needed() -> None:
    global _ring
    if _ring is not None:
        return
    try:
        _ring = SharedFrameRing.attach(
            shm_name=_icfg.shm_name,
            frame_sem_name=_icfg.frame_sem_name,
            mutex_sem_name=_icfg.mutex_sem_name,
        )
        LOGGER.info("Connected to frame ring.")
    except Exception as exc:
        LOGGER.warning("Frame ring not available yet: %s", exc)
        _ring = None


def _attach_command_if_needed() -> None:
    global _command_channel, _command_sender
    if _command_sender is not None and _command_channel is not None:
        return
    try:
        _command_channel = SharedTextChannel.attach(
            shm_name=_icfg.command_shm_name,
            mutex_sem_name=_icfg.command_mutex_sem_name,
            notify_sem_name=_icfg.command_ready_sem_name,
        )
        _command_sender = CommandSender(_command_channel)
        LOGGER.info("Connected to command channel.")
    except Exception as exc:
        LOGGER.warning("Command channel not available yet: %s", exc)
        _command_channel = None
        _command_sender = None


def _refresh_ipc_connections() -> None:
    _attach_ring_if_needed()
    _attach_command_if_needed()


def _build_ring_reader() -> SharedFrameRing:
    return SharedFrameRing.attach(
        shm_name=_icfg.shm_name,
        frame_sem_name=_icfg.frame_sem_name,
        mutex_sem_name=_icfg.mutex_sem_name,
    )


async def _run_webrtc_signaling(websocket: WebSocket, *, use_testsrc: bool) -> None:
    await websocket.accept()

    if not webrtc_available():
        details = webrtc_error_message()
        LOGGER.warning("Rejecting WebRTC signaling connection: aiortc unavailable: %s", details)
        await websocket.send_json(
            {
                "type": "error",
                "message": "aiortc WebRTC runtime is unavailable.",
                "details": details,
            }
        )
        await websocket.close(code=1011)
        return

    if not use_testsrc:
        _attach_ring_if_needed()
        if _ring is None:
            LOGGER.warning("Rejecting WebRTC signaling connection: capture pipeline is not connected.")
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Capture pipeline is not connected. Start capture_pipeline first.",
                }
            )
            await websocket.close(code=1013)
            return

    session = WebRtcSession(
        loop=asyncio.get_running_loop(),
        ring_factory=None if use_testsrc else _build_ring_reader,
        video_config=_vcfg,
        webrtc_config=_resolved_wcfg,
        use_testsrc=use_testsrc,
    )

    try:
        session.start()
    except (RuntimeError, WebRtcDependencyError) as exc:
        LOGGER.exception("Failed to start WebRTC session.")
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close(code=1011)
        return

    sender_task = asyncio.create_task(_forward_session_events(session, websocket))
    try:
        await websocket.send_json(
            {
                "type": "hello",
                "message": "webrtc_signaling_ready",
                "session": "server-offer",
                "source": "videotestsrc" if use_testsrc else "ipc-ring",
            }
        )
        while True:
            try:
                payload = await websocket.receive_json()
                await _handle_signaling_message(session, payload)
            except ValueError as exc:
                await websocket.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        LOGGER.info("WebRTC signaling client disconnected.")
    finally:
        sender_task.cancel()
        await asyncio.gather(sender_task, return_exceptions=True)
        session.stop()


def encode_jpeg(frame) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


@app.on_event("startup")
def startup() -> None:
    _refresh_ipc_connections()
    if not webrtc_available():
        LOGGER.warning("WebRTC runtime unavailable: %s", webrtc_error_message())


@app.on_event("shutdown")
def shutdown() -> None:
    global _ring, _command_channel, _command_sender
    if _ring is not None:
        _ring.close()
        _ring = None
    if _command_channel is not None:
        _command_channel.close()
        _command_channel = None
    _command_sender = None


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/v1/status", response_model=ApiStatusResponse)
def status_info() -> ApiStatusResponse:
    _refresh_ipc_connections()
    ring_connected = _ring is not None
    command_connected = _command_sender is not None
    webrtc_ok = webrtc_available()
    ok = ring_connected and command_connected and webrtc_ok
    if ok:
        message = "ready"
    elif not webrtc_ok:
        message = "webrtc_runtime_unavailable"
    else:
        message = "waiting_for_capture_pipeline"
    return ApiStatusResponse(
        ok=ok,
        ring_connected=ring_connected,
        command_connected=command_connected,
        webrtc_available=webrtc_ok,
        message=message,
    )


@app.get("/v1/frame.jpg")
def snapshot() -> Response:
    global _ring
    _attach_ring_if_needed()
    if _ring is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "frame_ring_unavailable",
                "message": "Capture pipeline is not connected. Start capture_pipeline first.",
                "details": {},
            },
        )
    try:
        _, frame = _ring.read_latest_frame()
        jpg = encode_jpeg(frame)
    except Exception as exc:
        _ring = None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "frame_unavailable",
                "message": "Frame not available yet. Try again.",
                "details": {"error": str(exc)},
            },
        )
    return Response(content=jpg, media_type="image/jpeg")


def _send_camera_command(command: str) -> CommandResponse:
    global _command_channel, _command_sender
    _attach_command_if_needed()
    if _command_sender is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "ipc_command_unavailable",
                "message": "IPC command channel is not connected. Start capture_pipeline first.",
                "details": {},
            },
        )

    request_id = str(uuid4())
    try:
        _command_sender.send(command=command, request_id=request_id)
    except Exception as exc:
        LOGGER.warning("Command send failed, channel will be retried: %s", exc)
        _command_channel = None
        _command_sender = None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "ipc_command_send_failed",
                "message": "IPC command channel is temporarily unavailable. Try again.",
                "details": {"error": str(exc)},
            },
        )
    return CommandResponse(
        ok=True,
        command=command,
        request_id=request_id,
        message="Command queued.",
    )


@app.post("/v1/camera/start", response_model=CommandResponse, status_code=status.HTTP_202_ACCEPTED)
def camera_start() -> CommandResponse:
    return _send_camera_command("camera_start")


@app.post("/v1/camera/stop", response_model=CommandResponse, status_code=status.HTTP_202_ACCEPTED)
def camera_stop() -> CommandResponse:
    return _send_camera_command("camera_stop")


@app.websocket(_resolved_wcfg.signaling_path)
async def webrtc_signaling(websocket: WebSocket) -> None:
    await _run_webrtc_signaling(websocket, use_testsrc=False)


@app.websocket("/ws/webrtc-testsrc")
async def webrtc_signaling_testsrc(websocket: WebSocket) -> None:
    await _run_webrtc_signaling(websocket, use_testsrc=True)


async def _forward_session_events(session: WebRtcSession, websocket: WebSocket) -> None:
    while True:
        event = await session.next_event()
        message = {"type": event.type}
        message.update(event.payload)
        await websocket.send_json(message)


async def _handle_signaling_message(session: WebRtcSession, payload: dict) -> None:
    msg_type = payload.get("type")
    if msg_type == "answer":
        sdp = payload.get("sdp")
        if not isinstance(sdp, str):
            raise ValueError("answer.sdp must be a string")
        LOGGER.info("Received WebRTC answer from client.")
        session.set_remote_answer(sdp)
        return

    if msg_type == "ice-candidate":
        candidate = payload.get("candidate")
        sdp_mline_index = payload.get("sdpMLineIndex", 0)
        if not isinstance(candidate, str):
            raise ValueError("ice-candidate.candidate must be a string")
        LOGGER.info("Received remote ICE candidate for mline %s.", sdp_mline_index)
        session.add_ice_candidate(int(sdp_mline_index), candidate)
        return

    if msg_type == "ping":
        return

    raise ValueError(f"Unsupported signaling message type: {msg_type}")
