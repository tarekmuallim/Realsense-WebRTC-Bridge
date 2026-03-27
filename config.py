"""
Project configuration.

Keep compute low on Orin Nano:
- start with 640x480 @ 30 fps
- grayscale IR (1 channel) is cheaper than BGR
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Type, TypeVar, Union


VIDEO_SOURCES = {"color", "left_ir", "right_ir", "depth", "colored_depth"}


@dataclass(frozen=True)
class VideoConfig:
    """Video/capture settings.

    Invalid values fail fast via __post_init__ validation.
    """

    width: int = 640
    height: int = 480
    source: str = "color"
    fps: int = 30
    ring_size: int = 4

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be > 0")
        if self.source not in VIDEO_SOURCES:
            raise ValueError("source must be one of: color, left_ir, right_ir, depth, colored_depth")
        if self.fps <= 0:
            raise ValueError("fps must be > 0")
        if self.ring_size <= 0:
            raise ValueError("ring_size must be > 0")

    @property
    def channels(self) -> int:
        return 3 if self.source in {"color", "colored_depth"} else 1


@dataclass(frozen=True)
class WebRtcConfig:
    """WebRTC sender settings for the FastAPI bridge."""

    signaling_path: str = "/ws/webrtc"

    # preferred_video_codec: Optional[str] = None  # "vp8", "h264", or None for default negotiation order
    # quality_profile: str = "custom"  # "custom", "low", "medium", "high"
    # max_bitrate_bps: Optional[int] = None
    # min_bitrate_bps: Optional[int] = None
    # start_bitrate_bps: Optional[int] = None
    # max_framerate: Optional[float] = None

    preferred_video_codec: Optional[str] = 'vp8'  # "vp8", "h264", or None for default negotiation order
    quality_profile: str = "low"  # "custom", "low", "medium", "high"
    max_bitrate_bps: Optional[int] = None
    min_bitrate_bps: Optional[int] = None
    start_bitrate_bps: Optional[int] = None
    max_framerate: Optional[float] = None
    
    # preferred_video_codec: Optional[str] = "h264"
    # quality_profile: str = "high"
    # max_bitrate_bps: Optional[int] = 1_500_000
    # min_bitrate_bps: Optional[int] = 400_000
    # start_bitrate_bps: Optional[int] = 800_000
    # max_framerate: Optional[float] = 20.0

    scale_resolution_down_by: float = 1.0

    def __post_init__(self) -> None:
        if not self.signaling_path.startswith("/"):
            raise ValueError("signaling_path must start with '/'")
        if self.preferred_video_codec is not None:
            codec = self.preferred_video_codec.strip().lower()
            if codec not in {"vp8", "h264"}:
                raise ValueError("preferred_video_codec must be 'vp8', 'h264', or None")
        if self.quality_profile not in {"custom", "low", "medium", "high"}:
            raise ValueError("quality_profile must be one of: custom, low, medium, high")
        for field_name in ("max_bitrate_bps", "min_bitrate_bps", "start_bitrate_bps"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be > 0 when set")
        if self.max_framerate is not None and self.max_framerate <= 0:
            raise ValueError("max_framerate must be > 0 when set")
        if self.scale_resolution_down_by < 1.0:
            raise ValueError("scale_resolution_down_by must be >= 1.0")
        if (
            self.min_bitrate_bps is not None
            and self.max_bitrate_bps is not None
            and self.min_bitrate_bps > self.max_bitrate_bps
        ):
            raise ValueError("min_bitrate_bps cannot be greater than max_bitrate_bps")

    def resolved(self) -> "ResolvedWebRtcConfig":
        profile_defaults = {
            "custom": {"max_bitrate_bps": None, "max_framerate": None, "scale_resolution_down_by": 1.0},
            "low": {"max_bitrate_bps": 500_000, "max_framerate": 15.0, "scale_resolution_down_by": 2.0},
            "medium": {"max_bitrate_bps": 1_200_000, "max_framerate": 24.0, "scale_resolution_down_by": 1.25},
            "high": {"max_bitrate_bps": 2_500_000, "max_framerate": 30.0, "scale_resolution_down_by": 1.0},
        }[self.quality_profile]
        preferred_video_codec = (
            None if self.preferred_video_codec is None else self.preferred_video_codec.strip().lower()
        )
        max_bitrate_bps = self.max_bitrate_bps
        max_framerate = self.max_framerate
        scale_resolution_down_by = self.scale_resolution_down_by
        if max_bitrate_bps is None:
            max_bitrate_bps = profile_defaults["max_bitrate_bps"]
        if max_framerate is None:
            max_framerate = profile_defaults["max_framerate"]
        if scale_resolution_down_by == 1.0:
            scale_resolution_down_by = profile_defaults["scale_resolution_down_by"]
        return ResolvedWebRtcConfig(
            signaling_path=self.signaling_path,
            preferred_video_codec=preferred_video_codec,
            quality_profile=self.quality_profile,
            max_bitrate_bps=max_bitrate_bps,
            min_bitrate_bps=self.min_bitrate_bps,
            start_bitrate_bps=self.start_bitrate_bps,
            max_framerate=max_framerate,
            scale_resolution_down_by=scale_resolution_down_by,
        )


@dataclass(frozen=True)
class ResolvedWebRtcConfig:
    signaling_path: str
    preferred_video_codec: Optional[str]
    quality_profile: str
    max_bitrate_bps: Optional[int]
    min_bitrate_bps: Optional[int]
    start_bitrate_bps: Optional[int]
    max_framerate: Optional[float]
    scale_resolution_down_by: float


@dataclass(frozen=True)
class IpcConfig:
    shm_name: str = "rs_frames"          # multiprocessing.shared_memory name (no slash)
    frame_sem_name: str = "/rs_frame"    # POSIX semaphore name (leading slash)
    mutex_sem_name: str = "/rs_mutex"    # POSIX semaphore name (leading slash)
    command_shm_name: str = "rs_command"
    command_mutex_sem_name: str = "/rs_command_mutex"
    command_ready_sem_name: str = "/rs_command_ready"
    command_shm_size: int = 4096
    message_shm_name: str = "rs_message"
    message_mutex_sem_name: str = "/rs_message_mutex"
    message_ready_sem_name: str = "/rs_message_ready"
    message_shm_size: int = 4096

    def __post_init__(self) -> None:
        for field_name in (
            "frame_sem_name",
            "mutex_sem_name",
            "command_mutex_sem_name",
            "command_ready_sem_name",
            "message_mutex_sem_name",
            "message_ready_sem_name",
        ):
            value = getattr(self, field_name)
            if not value.startswith("/"):
                raise ValueError(f"{field_name} must start with '/'")
        for field_name in ("shm_name", "command_shm_name", "message_shm_name"):
            value = getattr(self, field_name)
            if not value or "/" in value:
                raise ValueError(f"{field_name} must be non-empty and must not contain '/'")
        for field_name in ("command_shm_size", "message_shm_size"):
            value = getattr(self, field_name)
            if value <= 0:
                raise ValueError(f"{field_name} must be > 0")


@dataclass(frozen=True)
class LoggingConfig:
    log_dir: str = "logs"
    file_name: str = "capture_pipeline.log"
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5

    def __post_init__(self) -> None:
        if not self.log_dir.strip():
            raise ValueError("log_dir must be non-empty")
        if not self.file_name.strip():
            raise ValueError("file_name must be non-empty")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        if self.backup_count < 0:
            raise ValueError("backup_count must be >= 0")
        self.resolved_level()

    def resolved_level(self) -> int:
        normalized = self.level.strip().upper()
        level_value = logging.getLevelName(normalized)
        if not isinstance(level_value, int):
            raise ValueError("level must be a valid logging level name such as DEBUG, INFO, WARNING, or ERROR")
        return level_value


@dataclass(frozen=True)
class AppConfig:
    video: VideoConfig
    webrtc: WebRtcConfig
    ipc: IpcConfig
    logging: LoggingConfig


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "settings.json"
ConfigPath = Union[str, Path]
ConfigType = TypeVar("ConfigType")


def _load_raw_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Configuration file not found: {path}. Create it from the repository default settings."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in configuration file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"Top-level configuration in {path} must be a JSON object")
    return raw


def _build_section(section_name: str, config_cls: Type[ConfigType], raw: dict[str, Any]) -> ConfigType:
    section = raw.get(section_name, {})
    if not isinstance(section, dict):
        raise ValueError(f"Configuration section '{section_name}' must be a JSON object")
    try:
        return config_cls(**section)
    except TypeError as exc:
        raise ValueError(f"Invalid keys in configuration section '{section_name}': {exc}") from exc


def load_app_config(path: ConfigPath = DEFAULT_CONFIG_PATH) -> AppConfig:
    config_path = Path(path)
    raw = _load_raw_config(config_path)

    known_sections = {"video", "webrtc", "ipc", "logging"}
    unknown_sections = sorted(set(raw) - known_sections)
    if unknown_sections:
        raise ValueError(
            f"Unknown top-level configuration sections in {config_path}: {', '.join(unknown_sections)}"
        )

    return AppConfig(
        video=_build_section("video", VideoConfig, raw),
        webrtc=_build_section("webrtc", WebRtcConfig, raw),
        ipc=_build_section("ipc", IpcConfig, raw),
        logging=_build_section("logging", LoggingConfig, raw),
    )


def load_video_config(path: ConfigPath = DEFAULT_CONFIG_PATH) -> VideoConfig:
    return load_app_config(path).video


def load_webrtc_config(path: ConfigPath = DEFAULT_CONFIG_PATH) -> WebRtcConfig:
    return load_app_config(path).webrtc


def load_ipc_config(path: ConfigPath = DEFAULT_CONFIG_PATH) -> IpcConfig:
    return load_app_config(path).ipc


def load_logging_config(path: ConfigPath = DEFAULT_CONFIG_PATH) -> LoggingConfig:
    return load_app_config(path).logging
