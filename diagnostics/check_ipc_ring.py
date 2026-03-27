from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_ipc_config
from ipc.shared_ring import SharedFrameRing


def main() -> int:
    parser = argparse.ArgumentParser(description="Check frame IPC ring attachment and frame reads.")
    parser.add_argument("--timeout", type=float, default=3.0, help="Seconds to wait for one frame.")
    args = parser.parse_args()

    cfg = load_ipc_config()
    ring = None
    try:
        ring = SharedFrameRing.attach(
            shm_name=cfg.shm_name,
            frame_sem_name=cfg.frame_sem_name,
            mutex_sem_name=cfg.mutex_sem_name,
        )
        print("PASS: attached to frame ring.")
        print(f"ring shm={ring.shm_name} ring_size={ring.ring_size} frame_bytes={ring.frame_bytes}")

        started = time.time()
        if not ring.wait_for_frame(timeout_s=args.timeout):
            print(f"FAIL: no frame received within {args.timeout:.1f}s.")
            return 1

        info, frame = ring.read_latest_frame()
        frame_shape = tuple(frame.shape)
        frame_id = info.frame_id
        width = info.width
        height = info.height
        channels = info.channels
        ts_ns = info.ts_ns
        del frame
        age_ms = (time.time_ns() - info.ts_ns) / 1_000_000.0
        del info
        print(
            "PASS: frame read "
            f"frame_id={frame_id} size={width}x{height}x{channels} "
            f"shape={frame_shape} age_ms={age_ms:.1f} wait_s={time.time() - started:.3f}"
        )
        return 0
    except Exception as exc:
        print(f"FAIL: IPC ring check failed: {exc}")
        return 1
    finally:
        if ring is not None:
            ring.close()


if __name__ == "__main__":
    raise SystemExit(main())
