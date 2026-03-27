"""
RealSense camera wrapper.

Supported sources: color, left_ir, right_ir, depth, colored_depth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

try:
    import pyrealsense2 as rs
except Exception:  # pragma: no cover
    rs = None


@dataclass
class RealSenseConfig:
    width: int
    height: int
    fps: int
    source: str = "color"

    def __post_init__(self) -> None:
        if self.source not in {"color", "left_ir", "right_ir", "depth", "colored_depth"}:
            raise ValueError("source must be one of: color, left_ir, right_ir, depth, colored_depth")


class RealSenseCamera:
    """Single-stream RealSense capture."""

    def __init__(self, cfg: RealSenseConfig) -> None:
        self._cfg = cfg
        self._pipeline: Optional["rs.pipeline"] = None

    def start(self) -> None:
        if rs is None:
            raise RuntimeError("pyrealsense2 not available.")
        self._pipeline = rs.pipeline()
        config = rs.config()

        if self._cfg.source == "left_ir":
            config.enable_stream(
                rs.stream.infrared,
                1,
                self._cfg.width,
                self._cfg.height,
                rs.format.y8,
                self._cfg.fps,
            )
        elif self._cfg.source == "right_ir":
            config.enable_stream(
                rs.stream.infrared,
                2,
                self._cfg.width,
                self._cfg.height,
                rs.format.y8,
                self._cfg.fps,
            )
        elif self._cfg.source in {"depth", "colored_depth"}:
            config.enable_stream(
                rs.stream.depth,
                self._cfg.width,
                self._cfg.height,
                rs.format.z16,
                self._cfg.fps,
            )
        elif self._cfg.source == "color":
            config.enable_stream(
                rs.stream.color,
                self._cfg.width,
                self._cfg.height,
                rs.format.bgr8,
                self._cfg.fps,
            )
        else:
            raise ValueError(f"Unsupported RealSense source: {self._cfg.source}")

        self._pipeline.start(config)

    def read(self) -> np.ndarray:
        if self._pipeline is None:
            raise RuntimeError("Camera not started.")
        frames = self._pipeline.wait_for_frames()

        if self._cfg.source == "left_ir":
            f = frames.get_infrared_frame(1)
        elif self._cfg.source == "right_ir":
            f = frames.get_infrared_frame(2)
        elif self._cfg.source in {"depth", "colored_depth"}:
            f = frames.get_depth_frame()
        elif self._cfg.source == "color":
            f = frames.get_color_frame()
        else:
            raise ValueError(f"Unsupported RealSense source: {self._cfg.source}")

        if not f:
            raise RuntimeError("Failed to read frame.")
        frame = np.asanyarray(f.get_data())
        if self._cfg.source == "depth":
            # Compress 16-bit depth to 8-bit for the current shared-ring/WebRTC pipeline.
            frame = cv2.convertScaleAbs(frame, alpha=255.0 / 10000.0)
        elif self._cfg.source == "colored_depth":
            depth_8bit = cv2.convertScaleAbs(frame, alpha=255.0 / 10000.0)
            frame = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
        return frame

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
