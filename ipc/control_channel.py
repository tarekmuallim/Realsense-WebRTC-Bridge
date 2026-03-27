"""
Structured control IPC:
- Outbound capture-pipeline messages (status/warning/error)
- Inbound control commands
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ipc.shared_text_channel import SharedTextChannel

MESSAGE_LEVEL_STATUS = "status"
MESSAGE_LEVEL_WARNING = "warning"
MESSAGE_LEVEL_ERROR = "error"
VALID_MESSAGE_LEVELS = {
    MESSAGE_LEVEL_STATUS,
    MESSAGE_LEVEL_WARNING,
    MESSAGE_LEVEL_ERROR,
}


@dataclass(frozen=True)
class ControlMessage:
    level: str
    message: str
    ts_ns: int = field(default_factory=time.time_ns)
    source: str = "capture_pipeline"
    details: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        if self.level not in VALID_MESSAGE_LEVELS:
            raise ValueError(f"Invalid message level: {self.level}")
        payload = {
            "type": "message",
            "level": self.level,
            "message": self.message,
            "ts_ns": self.ts_ns,
            "source": self.source,
            "details": self.details,
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "ControlMessage":
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("Invalid control message payload type.")
        return cls(
            level=str(payload["level"]),
            message=str(payload["message"]),
            ts_ns=int(payload.get("ts_ns", time.time_ns())),
            source=str(payload.get("source", "unknown")),
            details=dict(payload.get("details") or {}),
        )


@dataclass(frozen=True)
class ControlCommand:
    command: str
    args: Dict[str, Any] = field(default_factory=dict)
    ts_ns: int = field(default_factory=time.time_ns)
    request_id: str = ""

    def to_json(self) -> str:
        payload = {
            "type": "command",
            "command": self.command,
            "args": self.args,
            "ts_ns": self.ts_ns,
            "request_id": self.request_id,
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "ControlCommand":
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("Invalid control command payload type.")
        command = str(payload["command"])
        if not command:
            raise ValueError("Control command is empty.")
        return cls(
            command=command,
            args=dict(payload.get("args") or {}),
            ts_ns=int(payload.get("ts_ns", time.time_ns())),
            request_id=str(payload.get("request_id", "")),
        )


class MessagePublisher:
    """Thin wrapper around shared message channel."""

    def __init__(self, channel: SharedTextChannel) -> None:
        self._channel = channel

    def publish(
        self,
        level: str,
        message: str,
        *,
        source: str = "capture_pipeline",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        msg = ControlMessage(level=level, message=message, source=source, details=details or {})
        self._channel.send(msg.to_json())


class CommandReceiver:
    """Thin wrapper around shared command channel."""

    def __init__(self, channel: SharedTextChannel) -> None:
        self._channel = channel

    def try_receive(self, timeout_s: float = 0.0) -> Optional[ControlCommand]:
        raw = self._channel.try_receive(timeout_s=timeout_s)
        if raw is None:
            return None
        return ControlCommand.from_json(raw)


class CommandSender:
    """Thin wrapper around shared command channel."""

    def __init__(self, channel: SharedTextChannel) -> None:
        self._channel = channel

    def send(
        self,
        command: str,
        *,
        args: Optional[Dict[str, Any]] = None,
        request_id: str = "",
    ) -> None:
        if not command:
            raise ValueError("command must be a non-empty string")
        payload = ControlCommand(command=command, args=args or {}, request_id=request_id)
        self._channel.send(payload.to_json())
