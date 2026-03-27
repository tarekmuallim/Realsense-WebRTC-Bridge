"""
Explicit cleanup utilities.

Run this after crashes, or call from capture-pipeline shutdown.
"""
from __future__ import annotations

from multiprocessing import shared_memory
import posix_ipc


def unlink_shared_memory(name: str) -> None:
    try:
        shm = shared_memory.SharedMemory(name=name, create=False)
        shm.close()
        shm.unlink()
    except FileNotFoundError:
        pass


def unlink_semaphore(name: str) -> None:
    try:
        posix_ipc.unlink_semaphore(name)
    except posix_ipc.ExistentialError:
        pass
