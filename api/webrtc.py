from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription, VideoStreamTrack
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
from av import VideoFrame

from config import ResolvedWebRtcConfig, VideoConfig
from ipc.shared_ring import SharedFrameRing

LOGGER = logging.getLogger(__name__)


async def _run_blocking(func: Callable[..., Any], *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


class WebRtcDependencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionEvent:
    type: str
    payload: dict[str, Any]


def webrtc_available() -> bool:
    return True


def webrtc_error_message() -> str:
    return ""


class TestPatternTrack(VideoStreamTrack):
    def __init__(self, video_config: VideoConfig, webrtc_config: ResolvedWebRtcConfig) -> None:
        super().__init__()
        self._video_config = video_config
        self._webrtc_config = webrtc_config
        self._frame_index = 0
        self._last_send_at = 0.0

    async def recv(self) -> VideoFrame:
        await self._throttle()
        pts, time_base = await self.next_timestamp()
        frame = self._prepare_frame(self._make_frame())
        video = VideoFrame.from_ndarray(frame, format="bgr24")
        video.pts = pts
        video.time_base = time_base
        return video

    def _make_frame(self) -> np.ndarray:
        height = self._video_config.height
        width = self._video_config.width
        x = np.linspace(0, 255, width, dtype=np.uint8)
        y = np.linspace(0, 255, height, dtype=np.uint8)
        xv = np.tile(x, (height, 1))
        yv = np.tile(y[:, None], (1, width))
        phase = (self._frame_index * 4) % 255
        self._frame_index += 1
        frame = np.empty((height, width, 3), dtype=np.uint8)
        frame[..., 0] = xv
        frame[..., 1] = yv
        frame[..., 2] = (xv // 2 + phase) % 255
        cv2.putText(
            frame,
            f"aiortc testsrc {self._frame_index}",
            (24, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        return frame

    async def _throttle(self) -> None:
        max_framerate = self._webrtc_config.max_framerate
        if max_framerate is None:
            return
        min_period = 1.0 / max_framerate
        now = time.monotonic()
        if self._last_send_at > 0.0:
            remaining = min_period - (now - self._last_send_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_send_at = time.monotonic()

    def _prepare_frame(self, frame: np.ndarray) -> np.ndarray:
        return _resize_frame(frame, self._webrtc_config.scale_resolution_down_by)


class RingVideoTrack(VideoStreamTrack):
    def __init__(
        self,
        *,
        ring_factory: Callable[[], SharedFrameRing],
        events: asyncio.Queue[SessionEvent],
        webrtc_config: ResolvedWebRtcConfig,
    ) -> None:
        super().__init__()
        self._ring_factory = ring_factory
        self._event_queue = events
        self._webrtc_config = webrtc_config
        self._ring: Optional[SharedFrameRing] = None
        self._last_frame_id = -1
        self._frames_sent = 0
        self._last_send_at = 0.0

    async def recv(self) -> VideoFrame:
        await self._throttle()
        pts, time_base = await self.next_timestamp()

        while self.readyState == "live":
            try:
                info, frame = await self._get_latest_unique_frame()
            except Exception as exc:
                await self._event_queue.put(
                    SessionEvent(type="status", payload={"message": f"frame_read_error:{type(exc).__name__}:{exc}"})
                )
                await asyncio.sleep(0.1)
                continue

            self._last_frame_id = info.frame_id
            frame = np.ascontiguousarray(_resize_frame(frame.copy(), self._webrtc_config.scale_resolution_down_by))
            if info.channels == 1:
                video = VideoFrame.from_ndarray(frame, format="gray")
            else:
                video = VideoFrame.from_ndarray(frame, format="bgr24")
            video.pts = pts
            video.time_base = time_base
            self._frames_sent += 1
            if self._frames_sent == 1:
                await self._event_queue.put(
                    SessionEvent(type="status", payload={"message": f"first_frame_sent:{info.frame_id}"})
                )
            elif self._frames_sent % 60 == 0:
                await self._event_queue.put(
                    SessionEvent(type="status", payload={"message": f"frames_sent:{self._frames_sent}"})
                )
            return video

        raise RuntimeError("Ring video track is no longer live.")

    async def stop_track(self) -> None:
        self.stop()
        if self._ring is not None:
            self._ring.close()
            self._ring = None

    async def _ensure_ring(self) -> SharedFrameRing:
        if self._ring is not None:
            return self._ring
        self._ring = await _run_blocking(self._ring_factory)
        await self._event_queue.put(SessionEvent(type="status", payload={"message": "frame_ring_connected"}))
        return self._ring

    async def _throttle(self) -> None:
        max_framerate = self._webrtc_config.max_framerate
        if max_framerate is None:
            return
        min_period = 1.0 / max_framerate
        now = time.monotonic()
        if self._last_send_at > 0.0:
            remaining = min_period - (now - self._last_send_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_send_at = time.monotonic()

    async def _get_latest_unique_frame(self) -> tuple[Any, np.ndarray]:
        ring = await self._ensure_ring()
        deadline = asyncio.get_running_loop().time() + 1.0
        while self.readyState == "live":
            info, frame = await _run_blocking(ring.read_latest_frame)
            if info.frame_id != self._last_frame_id:
                return info, frame
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Timed out waiting for a new ring frame.")
            await asyncio.sleep(0.01)
        raise RuntimeError("Ring video track is no longer live.")


class WebRtcSession:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        ring_factory: Optional[Callable[[], SharedFrameRing]],
        video_config: VideoConfig,
        webrtc_config: ResolvedWebRtcConfig,
        use_testsrc: bool = False,
    ) -> None:
        self._loop = loop
        self._ring_factory = ring_factory
        self._video_config = video_config
        self._webrtc_config = webrtc_config
        self._use_testsrc = use_testsrc
        self._events: asyncio.Queue[SessionEvent] = asyncio.Queue()
        self._pc = RTCPeerConnection()
        self._track: Optional[VideoStreamTrack] = None

    def start(self) -> None:
        if self._use_testsrc:
            self._track = TestPatternTrack(self._video_config, self._webrtc_config)
        else:
            if self._ring_factory is None:
                raise WebRtcDependencyError("ring_factory is required for IPC streaming")
            self._track = RingVideoTrack(
                ring_factory=self._ring_factory,
                events=self._events,
                webrtc_config=self._webrtc_config,
            )

        sender = self._pc.addTrack(self._track)
        self._apply_codec_preferences(sender)
        self._register_handlers()
        self._loop.create_task(self._create_offer())

    async def next_event(self) -> SessionEvent:
        return await self._events.get()

    def stop(self) -> None:
        self._loop.create_task(self._close())

    def set_remote_answer(self, sdp_text: str) -> None:
        self._loop.create_task(self._set_remote_answer(sdp_text))

    def add_ice_candidate(self, sdp_mline_index: int, candidate: str) -> None:
        self._loop.create_task(self._add_ice_candidate(sdp_mline_index, candidate))

    def _register_handlers(self) -> None:
        @self._pc.on("connectionstatechange")
        async def _() -> None:
            await self._events.put(
                SessionEvent(type="status", payload={"message": f"connection-state:{self._pc.connectionState}"})
            )

        @self._pc.on("iceconnectionstatechange")
        async def _() -> None:
            await self._events.put(
                SessionEvent(
                    type="status",
                    payload={"message": f"ice-connection-state:{self._pc.iceConnectionState}"},
                )
            )

        @self._pc.on("icegatheringstatechange")
        async def _() -> None:
            await self._events.put(
                SessionEvent(
                    type="status",
                    payload={"message": f"ice-gathering-state:{self._pc.iceGatheringState}"},
                )
            )

        @self._pc.on("signalingstatechange")
        async def _() -> None:
            await self._events.put(
                SessionEvent(type="status", payload={"message": f"signaling-state:{self._pc.signalingState}"})
            )

        @self._pc.on("icecandidate")
        async def on_icecandidate(candidate: Any) -> None:
            if candidate is None:
                return
            await self._events.put(
                SessionEvent(
                    type="ice-candidate",
                    payload={
                        "candidate": candidate_to_sdp(candidate),
                        "sdpMLineIndex": int(candidate.sdpMLineIndex or 0),
                    },
                )
            )

    async def _create_offer(self) -> None:
        try:
            offer = await self._pc.createOffer()
            offer = RTCSessionDescription(sdp=_apply_sdp_hints(offer.sdp, self._webrtc_config), type=offer.type)
            await self._pc.setLocalDescription(offer)
            await self._events.put(
                SessionEvent(
                    type="offer",
                    payload={"sdp": self._pc.localDescription.sdp},
                )
            )
            await self._events.put(SessionEvent(type="status", payload={"message": _describe_webrtc_config(self._webrtc_config)}))
        except Exception as exc:
            LOGGER.exception("Failed to create aiortc offer.")
            await self._events.put(SessionEvent(type="error", payload={"message": str(exc)}))

    async def _set_remote_answer(self, sdp_text: str) -> None:
        try:
            answer = RTCSessionDescription(sdp=sdp_text, type="answer")
            await self._pc.setRemoteDescription(answer)
            await self._events.put(SessionEvent(type="status", payload={"message": "remote_answer_applied"}))
        except Exception as exc:
            LOGGER.exception("Failed to apply remote answer.")
            await self._events.put(SessionEvent(type="error", payload={"message": str(exc)}))

    async def _add_ice_candidate(self, sdp_mline_index: int, candidate: str) -> None:
        try:
            ice = candidate_from_sdp(candidate)
            ice.sdpMLineIndex = sdp_mline_index
            await self._pc.addIceCandidate(ice)
        except Exception as exc:
            LOGGER.exception("Failed to add ICE candidate.")
            await self._events.put(SessionEvent(type="error", payload={"message": str(exc)}))

    async def _close(self) -> None:
        try:
            if isinstance(self._track, RingVideoTrack):
                await self._track.stop_track()
            elif self._track is not None:
                self._track.stop()
            await self._pc.close()
        except Exception:
            LOGGER.debug("aiortc session close failed.", exc_info=True)

    def _apply_codec_preferences(self, sender: RTCRtpSender) -> None:
        preferred_codec = self._webrtc_config.preferred_video_codec
        if preferred_codec is None:
            return
        transceiver = next((t for t in self._pc.getTransceivers() if t.sender == sender), None)
        if transceiver is None:
            LOGGER.warning("Unable to find transceiver for codec preference.")
            return
        codec_mime = f"video/{preferred_codec.upper()}"
        codecs = RTCRtpSender.getCapabilities("video").codecs
        if not codecs:
            LOGGER.warning("No video codecs reported by aiortc sender capabilities.")
            return
        ordered = sorted(codecs, key=lambda codec: _codec_sort_key(codec, codec_mime))
        if not any(codec.mimeType.lower() == codec_mime.lower() for codec in ordered):
            LOGGER.warning("Preferred codec %s is not available in aiortc capabilities.", codec_mime)
            return
        transceiver.setCodecPreferences(ordered)


def _codec_sort_key(codec: Any, preferred_mime_type: str) -> tuple[int, int, str]:
    mime_type = getattr(codec, "mimeType", "")
    name = mime_type.split("/", 1)[-1].lower()
    is_preferred = 0 if mime_type.lower() == preferred_mime_type.lower() else 1
    is_associated = 0 if name == "rtx" else 1
    return (is_preferred, is_associated, mime_type.lower())


def _resize_frame(frame: np.ndarray, scale_resolution_down_by: float) -> np.ndarray:
    if scale_resolution_down_by <= 1.0:
        return frame
    height, width = frame.shape[:2]
    target_width = max(1, int(round(width / scale_resolution_down_by)))
    target_height = max(1, int(round(height / scale_resolution_down_by)))
    if target_width == width and target_height == height:
        return frame
    return cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_AREA)


def _apply_sdp_hints(sdp: str, config: ResolvedWebRtcConfig) -> str:
    lines = sdp.splitlines()
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("m=") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)

    for index, section in enumerate(sections):
        if not section or not section[0].startswith("m=video "):
            continue
        sections[index] = _apply_video_section_hints(section, config)

    return "\r\n".join("\r\n".join(section) for section in sections) + "\r\n"


def _apply_video_section_hints(section: list[str], config: ResolvedWebRtcConfig) -> list[str]:
    payload_to_codec: dict[str, str] = {}
    for line in section:
        match = re.match(r"^a=rtpmap:(\d+)\s+([A-Za-z0-9\-]+)/", line)
        if match:
            payload_to_codec[match.group(1)] = match.group(2).lower()

    bitrate_params = []
    if config.min_bitrate_bps is not None:
        bitrate_params.append(f"x-google-min-bitrate={max(1, config.min_bitrate_bps // 1000)}")
    if config.start_bitrate_bps is not None:
        bitrate_params.append(f"x-google-start-bitrate={max(1, config.start_bitrate_bps // 1000)}")
    if config.max_bitrate_bps is not None:
        bitrate_params.append(f"x-google-max-bitrate={max(1, config.max_bitrate_bps // 1000)}")

    if config.max_bitrate_bps is not None:
        section = _upsert_bandwidth_line(section, config.max_bitrate_bps)
    if bitrate_params:
        section = _upsert_codec_bitrate_params(section, payload_to_codec, bitrate_params)
    return section


def _upsert_bandwidth_line(section: list[str], max_bitrate_bps: int) -> list[str]:
    updated: list[str] = []
    inserted = False
    for line in section:
        if line.startswith("b=AS:") or line.startswith("b=TIAS:"):
            continue
        updated.append(line)
        if not inserted and line.startswith("c="):
            updated.append(f"b=TIAS:{max_bitrate_bps}")
            inserted = True
    if not inserted:
        updated.insert(1, f"b=TIAS:{max_bitrate_bps}")
    return updated


def _upsert_codec_bitrate_params(
    section: list[str], payload_to_codec: dict[str, str], bitrate_params: list[str]
) -> list[str]:
    updated: list[str] = []
    seen_payloads: set[str] = set()
    target_codecs = {"vp8", "h264"}
    for line in section:
        match = re.match(r"^a=fmtp:(\d+)\s+(.*)$", line)
        if match and payload_to_codec.get(match.group(1)) in target_codecs:
            payload = match.group(1)
            existing = [param.strip() for param in match.group(2).split(";") if param.strip()]
            filtered = [param for param in existing if not param.startswith("x-google-")]
            filtered.extend(bitrate_params)
            updated.append(f"a=fmtp:{payload} {';'.join(filtered)}")
            seen_payloads.add(payload)
            continue
        updated.append(line)

    insertion_index = 0
    for idx, line in enumerate(updated):
        if line.startswith("a=rtpmap:"):
            insertion_index = idx + 1
    for payload, codec in payload_to_codec.items():
        if codec not in target_codecs or payload in seen_payloads:
            continue
        updated.insert(insertion_index, f"a=fmtp:{payload} {';'.join(bitrate_params)}")
        insertion_index += 1
    return updated


def _describe_webrtc_config(config: ResolvedWebRtcConfig) -> str:
    parts = [f"profile={config.quality_profile}"]
    if config.preferred_video_codec is not None:
        parts.append(f"codec={config.preferred_video_codec}")
    if config.max_bitrate_bps is not None:
        parts.append(f"max_bitrate_bps={config.max_bitrate_bps}")
    if config.min_bitrate_bps is not None:
        parts.append(f"min_bitrate_bps={config.min_bitrate_bps}")
    if config.start_bitrate_bps is not None:
        parts.append(f"start_bitrate_bps={config.start_bitrate_bps}")
    if config.max_framerate is not None:
        parts.append(f"max_framerate={config.max_framerate}")
    if config.scale_resolution_down_by > 1.0:
        parts.append(f"scale_resolution_down_by={config.scale_resolution_down_by}")
    return "webrtc_config:" + ",".join(parts)
