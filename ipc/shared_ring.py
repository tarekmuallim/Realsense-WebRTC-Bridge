"""
Shared-memory ring buffer for frames + POSIX semaphores.

Design goals:
- Single producer, multiple consumers.
- Producer overwrites old frames (low latency).
- Consumers read latest complete frame.

Header is protected by a mutex semaphore.
Frame availability is signaled with a counting semaphore.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from multiprocessing import resource_tracker, shared_memory
from typing import Optional, Tuple

import numpy as np
import posix_ipc

MAGIC = b"RSFM"
VERSION = 1

# Header:
# magic(4s), version(I), w(I), h(I), c(I), frame_bytes(I), ring(I), write_idx(I), frame_id(Q), ts_ns(Q)
HEADER_FMT = "<4sIIIIIIIQQ"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

# Slot meta:
# slot_frame_id(Q), slot_ts_ns(Q), valid(I)
SLOT_META_FMT = "<QQI"
SLOT_META_SIZE = struct.calcsize(SLOT_META_FMT)


@dataclass(frozen=True)
class FrameInfo:
    frame_id: int
    ts_ns: int
    width: int
    height: int
    channels: int


class SharedFrameRing:
    """
    Shared ring buffer containing raw frames.

    Producer:
      - create=True
      - writes frames
      - owns unlink on shutdown

    Consumer:
      - create=False
      - reads frames
      - only closes (no unlink)
    """

    def __init__(
        self,
        shm: shared_memory.SharedMemory,
        frame_sem: posix_ipc.Semaphore,
        mutex: posix_ipc.Semaphore,
        width: int,
        height: int,
        channels: int,
        ring_size: int,
        frame_bytes: int,
    ) -> None:
        self._shm = shm
        self._buf = shm.buf
        self._frame_sem = frame_sem
        self._mutex = mutex
        self._w = width
        self._h = height
        self._c = channels
        self._ring = ring_size
        self._frame_bytes = frame_bytes
        self._slot_size = SLOT_META_SIZE + frame_bytes

    @property
    def shm_name(self) -> str:
        return self._shm.name

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    @property
    def width(self) -> int:
        return self._w

    @property
    def height(self) -> int:
        return self._h

    @property
    def channels(self) -> int:
        return self._c

    @property
    def ring_size(self) -> int:
        return self._ring

    def close(self) -> None:
        self._shm.close()

    def unlink(self) -> None:
        self._shm.unlink()

    @staticmethod
    def calc_total_size(frame_bytes: int, ring_size: int) -> int:
        return HEADER_SIZE + ring_size * (SLOT_META_SIZE + frame_bytes)

    @staticmethod
    def _slot_offset(slot_index: int, slot_size: int) -> int:
        return HEADER_SIZE + slot_index * slot_size

    def _write_header(self, write_idx: int, frame_id: int, ts_ns: int) -> None:
        header = struct.pack(
            HEADER_FMT,
            MAGIC,
            VERSION,
            self._w,
            self._h,
            self._c,
            self._frame_bytes,
            self._ring,
            write_idx,
            frame_id,
            ts_ns,
        )
        self._buf[:HEADER_SIZE] = header

    def _read_header(self) -> Tuple[int, int, int, int, int, int, int, int, int, int]:
        return struct.unpack(HEADER_FMT, self._buf[:HEADER_SIZE])

    @classmethod
    def create(
        cls,
        shm_name: str,
        frame_sem_name: str,
        mutex_sem_name: str,
        width: int,
        height: int,
        channels: int,
        ring_size: int,
    ) -> "SharedFrameRing":
        frame_bytes = width * height * channels
        total_size = cls.calc_total_size(frame_bytes, ring_size)

        shm = shared_memory.SharedMemory(name=shm_name, create=True, size=total_size)

        frame_sem = posix_ipc.Semaphore(frame_sem_name, flags=posix_ipc.O_CREAT, initial_value=0)
        mutex = posix_ipc.Semaphore(mutex_sem_name, flags=posix_ipc.O_CREAT, initial_value=1)

        ring = cls(
            shm=shm,
            frame_sem=frame_sem,
            mutex=mutex,
            width=width,
            height=height,
            channels=channels,
            ring_size=ring_size,
            frame_bytes=frame_bytes,
        )

        # Initialize header
        mutex.acquire()
        try:
            ring._write_header(write_idx=0, frame_id=0, ts_ns=0)
        finally:
            mutex.release()

        return ring

    @classmethod
    def attach(
        cls,
        shm_name: str,
        frame_sem_name: str,
        mutex_sem_name: str,
    ) -> "SharedFrameRing":
        shm = shared_memory.SharedMemory(name=shm_name, create=False)
        try:
            resource_tracker.unregister(shm._name, "shared_memory")
        except Exception:
            pass
        frame_sem = posix_ipc.Semaphore(frame_sem_name)
        mutex = posix_ipc.Semaphore(mutex_sem_name)

        # Read header for dimensions
        header = struct.unpack(HEADER_FMT, shm.buf[:HEADER_SIZE])
        magic, ver, w, h, c, frame_bytes, ring_size, *_ = header
        if magic != MAGIC or ver != VERSION:
            raise RuntimeError("Shared memory header mismatch (magic/version).")

        return cls(
            shm=shm,
            frame_sem=frame_sem,
            mutex=mutex,
            width=w,
            height=h,
            channels=c,
            ring_size=ring_size,
            frame_bytes=frame_bytes,
        )

    # ---------- Producer API ----------

    def write_frame(self, frame: np.ndarray, frame_id: int) -> None:
        """
        Write a frame into the ring.

        frame: shape (H,W) if channels=1, else (H,W,C)
        """
        if frame.nbytes != self._frame_bytes:
            raise ValueError(f"Unexpected frame size: {frame.nbytes} != {self._frame_bytes}")

        ts_ns = time.time_ns()

        # Read current write index (protected)
        self._mutex.acquire()
        try:
            magic, ver, w, h, c, frame_bytes, ring, write_idx, *_ = self._read_header()
            # write into current slot
            off = self._slot_offset(write_idx, self._slot_size)
            meta = struct.pack(SLOT_META_FMT, frame_id, ts_ns, 1)
            self._buf[off : off + SLOT_META_SIZE] = meta
            self._buf[off + SLOT_META_SIZE : off + self._slot_size] = frame.tobytes()

            # advance write index and update header
            next_write_idx = (write_idx + 1) % self._ring
            self._write_header(next_write_idx, frame_id, ts_ns)
        finally:
            self._mutex.release()

        # Signal: one more frame available
        self._frame_sem.release()

    # ---------- Consumer API ----------

    def wait_for_frame(self, timeout_s: Optional[float] = None) -> bool:
        """
        Wait until at least one new frame is available.
        Returns True if acquired, False if timed out.
        """
        try:
            if timeout_s is None:
                self._frame_sem.acquire()
                return True
            self._frame_sem.acquire(timeout=timeout_s)
            return True
        except posix_ipc.BusyError:
            return False

    def read_latest_frame(self) -> Tuple[FrameInfo, np.ndarray]:
        """
        Read the most recent valid frame (low latency).
        """
        self._mutex.acquire()
        try:
            magic, ver, w, h, c, frame_bytes, ring, write_idx, frame_id, ts_ns = self._read_header()
        finally:
            self._mutex.release()

        newest_slot = (write_idx - 1) % self._ring
        off = self._slot_offset(newest_slot, self._slot_size)

        slot_frame_id, slot_ts_ns, valid = struct.unpack(
            SLOT_META_FMT, self._buf[off : off + SLOT_META_SIZE]
        )
        if valid != 1:
            raise RuntimeError("No valid frame in the newest slot yet.")

        data_view = self._buf[off + SLOT_META_SIZE : off + SLOT_META_SIZE + self._frame_bytes]
        arr = np.frombuffer(data_view, dtype=np.uint8)
        if self._c == 1:
            frame = arr.reshape((self._h, self._w))
        else:
            frame = arr.reshape((self._h, self._w, self._c))

        info = FrameInfo(
            frame_id=int(slot_frame_id),
            ts_ns=int(slot_ts_ns),
            width=self._w,
            height=self._h,
            channels=self._c,
        )
        return info, frame
