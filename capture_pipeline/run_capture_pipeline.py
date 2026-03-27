"""
Capture pipeline process:
- start RealSense
- capture frames
- write into shared memory ring
- cleanup on SIGINT/SIGTERM
"""
from __future__ import annotations

import logging
import signal
import time
from typing import Optional

import numpy as np

from app_logging import configure_capture_pipeline_logging
from config import IpcConfig, VideoConfig, load_app_config
from ipc.control_channel import (
    CommandReceiver,
    MessagePublisher,
    MESSAGE_LEVEL_ERROR,
    MESSAGE_LEVEL_STATUS,
    MESSAGE_LEVEL_WARNING,
)
from ipc.cleanup import unlink_semaphore, unlink_shared_memory
from ipc.shared_ring import SharedFrameRing
from ipc.shared_text_channel import SharedTextChannel

from camera.realsense_camera import RealSenseCamera, RealSenseConfig

LOGGER = logging.getLogger(__name__)


class CapturePipelineApp:
    def __init__(self, vcfg: VideoConfig, icfg: IpcConfig) -> None:
        self._vcfg = vcfg
        self._icfg = icfg
        self._ring: Optional[SharedFrameRing] = None
        self._command_channel: Optional[SharedTextChannel] = None
        self._message_channel: Optional[SharedTextChannel] = None
        self._command_receiver: Optional[CommandReceiver] = None
        self._message_publisher: Optional[MessagePublisher] = None
        self._cam: Optional[RealSenseCamera] = None
        self._running = False
        self._stopped = False

    def start(self) -> None:
        # In case of stale resources from earlier crash:
        self.cleanup_ipc()

        self._ring = SharedFrameRing.create(
            shm_name=self._icfg.shm_name,
            frame_sem_name=self._icfg.frame_sem_name,
            mutex_sem_name=self._icfg.mutex_sem_name,
            width=self._vcfg.width,
            height=self._vcfg.height,
            channels=self._vcfg.channels,
            ring_size=self._vcfg.ring_size,
        )
        self._command_channel = SharedTextChannel.create(
            shm_name=self._icfg.command_shm_name,
            mutex_sem_name=self._icfg.command_mutex_sem_name,
            notify_sem_name=self._icfg.command_ready_sem_name,
            size_bytes=self._icfg.command_shm_size,
        )
        self._message_channel = SharedTextChannel.create(
            shm_name=self._icfg.message_shm_name,
            mutex_sem_name=self._icfg.message_mutex_sem_name,
            notify_sem_name=self._icfg.message_ready_sem_name,
            size_bytes=self._icfg.message_shm_size,
        )
        self._command_receiver = CommandReceiver(self._command_channel)
        self._message_publisher = MessagePublisher(self._message_channel)
        LOGGER.info("Capture pipeline IPC channels initialized.")

        # Camera
        self._cam = RealSenseCamera(
            RealSenseConfig(
                width=self._vcfg.width,
                height=self._vcfg.height,
                fps=self._vcfg.fps,
                source=self._vcfg.source,
            )
        )

        try:
            self._cam.start()
            LOGGER.info("Camera started on pipeline startup.")
            self._publish(
                MESSAGE_LEVEL_STATUS,
                "camera_started",
                details={
                    "width": self._vcfg.width,
                    "height": self._vcfg.height,
                    "fps": self._vcfg.fps,
                    "source": self._vcfg.source,
                },
            )
        except Exception as exc:
            # Dev fallback: dummy frames if RealSense not available
            LOGGER.warning("RealSense start failed, using dummy frames: %s", exc)
            self._publish(MESSAGE_LEVEL_WARNING, "camera_start_failed_fallback_dummy", details={"error": str(exc)})
            self._cam = None

        self._running = True
        LOGGER.info("Capture pipeline loop starting.")
        self._publish(MESSAGE_LEVEL_STATUS, "capture_pipeline_started")

    def run(self) -> None:
        if self._ring is None:
            raise RuntimeError("Shared frame ring is not initialized. Call start() before run().")
        frame_id = 0
        period = 1.0 / max(1, self._vcfg.fps)

        while self._running:
            t0 = time.time()
            self._drain_commands()

            if self._cam is None:
                frame = self._make_dummy_frame()
            else:
                try:
                    frame = self._cam.read()
                except Exception as exc:
                    LOGGER.exception("Camera read failed, switching to dummy frames.")
                    self._publish(MESSAGE_LEVEL_ERROR, "camera_read_failed_switch_dummy", details={"error": str(exc)})
                    self._cam.stop()
                    self._cam = None
                    continue

            try:
                self._ring.write_frame(frame, frame_id)
                frame_id += 1
            except Exception as exc:
                LOGGER.exception("Ring write failed, frame dropped.")
                self._publish(
                    MESSAGE_LEVEL_ERROR,
                    "ring_write_failed_frame_dropped",
                    details={"frame_id": frame_id, "error": str(exc)},
                )
                continue

            # fps throttle
            dt = time.time() - t0
            sleep_s = max(0.0, period - dt)
            time.sleep(sleep_s)

    def _make_dummy_frame(self) -> np.ndarray:
        if self._vcfg.channels == 1:
            shape = (self._vcfg.height, self._vcfg.width)
        else:
            shape = (self._vcfg.height, self._vcfg.width, self._vcfg.channels)
        return np.random.randint(0, 255, shape, dtype=np.uint8)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._running = False
        LOGGER.info("Capture pipeline stop requested.")
        self._publish(MESSAGE_LEVEL_STATUS, "capture_pipeline_stopping")

        if self._cam is not None:
            self._cam.stop()

        if self._ring is not None:
            self._ring.close()
            # Capture pipeline owns unlink:
            try:
                self._ring.unlink()
            except FileNotFoundError:
                LOGGER.warning("Frame ring shared memory was already unlinked.")
            self._ring = None

        if self._command_channel is not None:
            self._command_channel.close()
            try:
                self._command_channel.unlink()
            except FileNotFoundError:
                LOGGER.warning("Command channel shared memory was already unlinked.")
            self._command_channel = None

        if self._message_channel is not None:
            self._message_channel.close()
            try:
                self._message_channel.unlink()
            except FileNotFoundError:
                LOGGER.warning("Message channel shared memory was already unlinked.")
            self._message_channel = None

        # Also remove semaphores for frame ring
        unlink_semaphore(self._icfg.frame_sem_name)
        unlink_semaphore(self._icfg.mutex_sem_name)

    def cleanup_ipc(self) -> None:
        unlink_shared_memory(self._icfg.shm_name)
        unlink_shared_memory(self._icfg.command_shm_name)
        unlink_shared_memory(self._icfg.message_shm_name)
        unlink_semaphore(self._icfg.frame_sem_name)
        unlink_semaphore(self._icfg.mutex_sem_name)
        unlink_semaphore(self._icfg.command_mutex_sem_name)
        unlink_semaphore(self._icfg.command_ready_sem_name)
        unlink_semaphore(self._icfg.message_mutex_sem_name)
        unlink_semaphore(self._icfg.message_ready_sem_name)

    def _publish(self, level: str, message: str, details: Optional[dict] = None) -> None:
        payload_details = details or {}
        if level == MESSAGE_LEVEL_ERROR:
            LOGGER.error("ipc_message=%s details=%s", message, payload_details)
        elif level == MESSAGE_LEVEL_WARNING:
            LOGGER.warning("ipc_message=%s details=%s", message, payload_details)
        else:
            LOGGER.info("ipc_message=%s details=%s", message, payload_details)

        if self._message_publisher is None:
            return
        self._message_publisher.publish(level=level, message=message, details=payload_details)

    def _drain_commands(self) -> None:
        if self._command_receiver is None:
            return

        while True:
            try:
                command = self._command_receiver.try_receive(timeout_s=0.0)
                if command is None:
                    return
            except Exception as exc:
                LOGGER.exception("Invalid command payload ignored.")
                self._publish(
                    MESSAGE_LEVEL_WARNING,
                    "invalid_command_payload_ignored",
                    details={"error": str(exc)},
                )
                continue
            LOGGER.info(
                "command_received command=%s request_id=%s args=%s",
                command.command,
                command.request_id,
                command.args,
            )
            self._handle_command(command.command, command.args, command.request_id)

    def _handle_command(self, command: str, args: dict, request_id: str) -> None:
        details = {"request_id": request_id, "args": args}
        if command == "ping":
            self._publish(MESSAGE_LEVEL_STATUS, "pong", details=details)
            return
        if command == "stop":
            self._publish(MESSAGE_LEVEL_STATUS, "stop_command_received", details=details)
            self._running = False
            return
        if command == "camera_start":
            self._handle_camera_start(details=details)
            return
        if command == "camera_stop":
            self._handle_camera_stop(details=details)
            return
        self._publish(
            MESSAGE_LEVEL_WARNING,
            "unknown_command",
            details={"request_id": request_id, "command": command, "args": args},
        )

    def request_stop(self) -> None:
        self._running = False

    def _handle_camera_start(self, details: dict) -> None:
        if self._cam is not None:
            self._publish(MESSAGE_LEVEL_WARNING, "camera_already_running", details=details)
            return

        cam = RealSenseCamera(
            RealSenseConfig(
                width=self._vcfg.width,
                height=self._vcfg.height,
                fps=self._vcfg.fps,
                source=self._vcfg.source,
            )
        )
        try:
            cam.start()
        except Exception as exc:
            LOGGER.exception("camera_start command failed.")
            self._publish(
                MESSAGE_LEVEL_ERROR,
                "camera_start_command_failed",
                details={**details, "error": str(exc)},
            )
            return

        self._cam = cam
        self._publish(MESSAGE_LEVEL_STATUS, "camera_started_by_command", details=details)

    def _handle_camera_stop(self, details: dict) -> None:
        if self._cam is None:
            self._publish(MESSAGE_LEVEL_WARNING, "camera_already_stopped", details=details)
            return

        self._cam.stop()
        self._cam = None
        self._publish(MESSAGE_LEVEL_STATUS, "camera_stopped_by_command", details=details)


def main() -> None:
    app_config = load_app_config()
    log_path = configure_capture_pipeline_logging(app_config.logging)
    LOGGER.info("Logging initialized. file=%s", log_path)
    vcfg = app_config.video
    icfg = app_config.ipc
    app = CapturePipelineApp(vcfg, icfg)

    def _handle(sig, frame) -> None:
        app.request_stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    app.start()
    try:
        app.run()
    finally:
        app.stop()


if __name__ == "__main__":
    main()
