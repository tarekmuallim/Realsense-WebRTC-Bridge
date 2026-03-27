"""
Cleanup script for shared memory and POSIX semaphores.

Run this if:
- Producer crashed
- You get "File exists" errors
- Shared memory is stuck in /dev/shm
"""

from config import load_ipc_config
from ipc.cleanup import unlink_shared_memory, unlink_semaphore


def main() -> None:
    cfg = load_ipc_config()

    print("Cleaning IPC resources...")

    unlink_shared_memory(cfg.shm_name)
    unlink_shared_memory(cfg.command_shm_name)
    unlink_shared_memory(cfg.message_shm_name)
    unlink_semaphore(cfg.frame_sem_name)
    unlink_semaphore(cfg.mutex_sem_name)
    unlink_semaphore(cfg.command_mutex_sem_name)
    unlink_semaphore(cfg.command_ready_sem_name)
    unlink_semaphore(cfg.message_mutex_sem_name)
    unlink_semaphore(cfg.message_ready_sem_name)

    print("Done.")


if __name__ == "__main__":
    main()
