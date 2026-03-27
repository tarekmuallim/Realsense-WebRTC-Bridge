"""
Shared-memory text channel with POSIX semaphores.

Layout:
- bytes[0:8]: unsigned 64-bit payload length
- bytes[8:8+N]: UTF-8 payload bytes
"""
from __future__ import annotations

import struct
from multiprocessing import shared_memory
from typing import Optional

import posix_ipc

LEN_FMT = "<Q"
LEN_SIZE = struct.calcsize(LEN_FMT)


class SharedTextChannel:
    """Single-slot string transport over shared memory."""

    def __init__(
        self,
        shm: shared_memory.SharedMemory,
        mutex: posix_ipc.Semaphore,
        notify: posix_ipc.Semaphore,
    ) -> None:
        self._shm = shm
        self._buf = shm.buf
        self._mutex = mutex
        self._notify = notify

    @classmethod
    def create(
        cls,
        shm_name: str,
        mutex_sem_name: str,
        notify_sem_name: str,
        size_bytes: int,
    ) -> "SharedTextChannel":
        shm = shared_memory.SharedMemory(name=shm_name, create=True, size=size_bytes)
        mutex = posix_ipc.Semaphore(mutex_sem_name, flags=posix_ipc.O_CREAT, initial_value=1)
        notify = posix_ipc.Semaphore(notify_sem_name, flags=posix_ipc.O_CREAT, initial_value=0)
        channel = cls(shm=shm, mutex=mutex, notify=notify)
        channel._initialize_empty()
        return channel

    @classmethod
    def attach(
        cls,
        shm_name: str,
        mutex_sem_name: str,
        notify_sem_name: str,
    ) -> "SharedTextChannel":
        shm = shared_memory.SharedMemory(name=shm_name, create=False)
        mutex = posix_ipc.Semaphore(mutex_sem_name)
        notify = posix_ipc.Semaphore(notify_sem_name)
        return cls(shm=shm, mutex=mutex, notify=notify)

    @property
    def shm_name(self) -> str:
        return self._shm.name

    @property
    def size_bytes(self) -> int:
        return self._shm.size

    def close(self) -> None:
        self._shm.close()

    def unlink(self) -> None:
        self._shm.unlink()
        try:
            posix_ipc.unlink_semaphore(self._mutex.name)
        except posix_ipc.ExistentialError:
            pass
        try:
            posix_ipc.unlink_semaphore(self._notify.name)
        except posix_ipc.ExistentialError:
            pass

    def drain_notifications(self) -> None:
        """Clear pending notify count so future reads start fresh."""
        try:
            while True:
                self._notify.acquire(timeout=0)
        except posix_ipc.BusyError:
            return

    def send(self, message: str) -> None:
        data = message.encode("utf-8")
        max_payload = self._shm.size - LEN_SIZE
        if len(data) > max_payload:
            raise ValueError(
                f"Message too large: {len(data)} bytes exceeds shared capacity {max_payload} bytes."
            )

        self._mutex.acquire()
        try:
            self._buf[:LEN_SIZE] = struct.pack(LEN_FMT, len(data))
            self._buf[LEN_SIZE : LEN_SIZE + len(data)] = data
        finally:
            self._mutex.release()

        self._notify.release()

    def try_receive(self, timeout_s: float = 0.0) -> Optional[str]:
        try:
            self._notify.acquire(timeout=timeout_s)
        except posix_ipc.BusyError:
            return None
        return self._read()

    def receive(self) -> str:
        self._notify.acquire()
        return self._read()

    def _initialize_empty(self) -> None:
        self._mutex.acquire()
        try:
            self._buf[:LEN_SIZE] = struct.pack(LEN_FMT, 0)
        finally:
            self._mutex.release()

    def _read(self) -> str:
        self._mutex.acquire()
        try:
            (payload_len,) = struct.unpack(LEN_FMT, self._buf[:LEN_SIZE])
            if payload_len > self._shm.size - LEN_SIZE:
                raise RuntimeError(f"Corrupted shared message length: {payload_len}")
            raw = self._buf[LEN_SIZE : LEN_SIZE + payload_len]
            return bytes(raw).decode("utf-8")
        finally:
            self._mutex.release()
