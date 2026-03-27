# RealSense WebRTC Bridge

This project captures video from an Intel RealSense camera, publishes frames through POSIX shared memory IPC, and serves those frames through a FastAPI application as:

- a browser-based WebRTC stream
- a JPEG snapshot endpoint
- simple HTTP control endpoints for camera start and stop
- WebSocket signaling for SDP and ICE exchange

The design separates camera capture from API serving:

- `capture_pipeline` owns the camera and writes frames into shared memory
- `api` reads the latest frame from shared memory and exposes streaming and control interfaces
- `ipc` provides the shared-memory and semaphore-based transport between those processes

This is useful when you want one process to talk to the RealSense camera while another process handles web APIs, browser signaling, and streaming.

## What The Program Uses

- Intel RealSense cameras
- Intel RealSense SDK / `librealsense`
- Python `pyrealsense2` wrapper
- POSIX shared memory and POSIX semaphores
- FastAPI for HTTP API and WebSocket signaling
- WebRTC via `aiortc`
- OpenCV and NumPy for frame handling

## High-Level Flow

1. The capture pipeline starts the RealSense camera.
2. Frames are written into a shared-memory ring buffer.
3. The API process attaches to the ring buffer.
4. A browser opens the FastAPI web page.
5. The page connects to a WebSocket signaling endpoint.
6. The API creates a WebRTC offer and sends video frames from the IPC ring.
7. The browser answers and exchanges ICE candidates with the API.

## Repository Structure

- `camera/`
  RealSense-specific camera wrapper.
- `capture_pipeline/`
  Producer process that captures frames and writes them into POSIX IPC.
- `ipc/`
  Shared-memory ring buffer, shared text channels, cleanup utilities, and control message helpers.
- `api/`
  FastAPI app, browser page, snapshot endpoint, control endpoints, and WebRTC signaling/streaming logic.
- `diagnostics/`
  Small scripts for checking IPC and HTTP behavior independently.
- `python_share_data_test_scripts/`
  Helper scripts for manually sending or receiving IPC messages.
- `settings.json`
  Runtime configuration loaded on program start.
- `config.py`
  Typed configuration loading and validation.

## Main Runtime Components

### 1. Capture Pipeline

Entry point:

```bash
python3 -m capture_pipeline.run_capture_pipeline
```

Responsibilities:

- loads `settings.json`
- configures logging
- opens the RealSense camera
- creates the frame ring and text channels
- writes frames into shared memory
- receives control commands from IPC
- publishes status, warning, and error messages
- cleans up shared memory and semaphores on shutdown

### 2. API Server

Entry point:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Responsibilities:

- loads `settings.json`
- attaches to the shared-memory ring and command channel
- serves the browser UI at `/`
- serves a JPEG snapshot at `/v1/frame.jpg`
- exposes camera control endpoints
- handles WebRTC offer/answer and ICE signaling
- streams frames from IPC to the browser through WebRTC

## Requirements

You need:

- an Intel RealSense camera supported by `librealsense`
- Linux with POSIX IPC support
- Python 3.9 or newer recommended
- the Intel RealSense SDK installed
- the Python wrapper `pyrealsense2`
- the Python packages listed in `requirements.txt`

Current Python dependencies in this repo:

- `fastapi`
- `uvicorn`
- `numpy`
- `opencv-python`
- `aiortc`
- `av`
- `posix_ipc`
- `pyrealsense2`

## RealSense SDK Installation

Install the Intel RealSense SDK before running this project.

Official references:

- Intel RealSense SDK repository:
  `https://github.com/IntelRealSense/librealsense`
- SDK installation and setup guides:
  `https://github.com/IntelRealSense/librealsense#installation`
- Python wrapper package:
  `https://pypi.org/project/pyrealsense2/`

Recommended approach:

1. Follow the official `librealsense` installation guide for your platform.
2. Verify that the camera is detected by the SDK tools.
3. Install the Python wrapper.
4. Install this repository's Python dependencies.

### Typical Linux / Ubuntu Flow

Use the official Intel RealSense installation guide for the exact commands for your platform and SDK version.

After the SDK is installed, create a virtual environment and install Python packages:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Important Note About `pyrealsense2`

The official `pyrealsense2` package is published on PyPI. The current PyPI package metadata lists Python `>=3.9`. If a wheel is available for your Python version and platform, `pip install -r requirements.txt` may be enough.

If `pyrealsense2` does not install cleanly on your machine, use the official `librealsense` repository guidance for your platform, especially the build-from-source or Jetson-specific setup paths.

## Configuration

All runtime settings are stored in [settings.json](./settings.json).

The file is loaded when each process starts. If you change `settings.json`, restart the process that should pick up the new values.

Configuration sections:

- `video`
- `webrtc`
- `ipc`
- `logging`

### `video`

Controls camera capture and frame ring sizing.

Fields:

- `width`
- `height`
- `fps`
- `ring_size`
- `source`

Supported `source` values:

- `color`
- `left_ir`
- `right_ir`
- `depth`
- `colored_depth`

Notes:

- `depth` is converted into an 8-bit grayscale frame for the current IPC/WebRTC pipeline.
- `colored_depth` applies a color map to the depth image and produces a 3-channel frame.

### `webrtc`

Controls API-side WebRTC behavior.

Fields:

- `signaling_path`
- `preferred_video_codec`
- `quality_profile`
- `max_bitrate_bps`
- `min_bitrate_bps`
- `start_bitrate_bps`
- `max_framerate`
- `scale_resolution_down_by`

### `ipc`

Controls POSIX shared-memory and semaphore names and sizes.

Fields:

- `shm_name`
- `frame_sem_name`
- `mutex_sem_name`
- `command_shm_name`
- `command_mutex_sem_name`
- `command_ready_sem_name`
- `command_shm_size`
- `message_shm_name`
- `message_mutex_sem_name`
- `message_ready_sem_name`
- `message_shm_size`

### `logging`

Controls capture-pipeline logging.

Fields:

- `log_dir`
- `file_name`
- `level`
- `max_bytes`
- `backup_count`

## API Summary

### Browser UI

- `GET /`
  Web page for live video, control buttons, and diagnostics.

## Web Interface

Opening `GET /` shows the built-in browser page for live viewing and diagnostics.

The page includes:

- a live WebRTC video viewer
- connect and disconnect controls
- camera start and stop controls
- stream status text
- basic stream statistics such as codec, bitrate, and frame rate
- a snapshot preview sourced from `/v1/frame.jpg`
- an on-page event log

For deeper WebRTC troubleshooting:

- the page also writes ICE, SDP, signaling, and related debug events to the browser console

### Status

- `GET /v1/status`
  Returns whether the API is connected to the frame ring and command channel and whether WebRTC is available.

### Snapshot

- `GET /v1/frame.jpg`
  Returns the latest frame as JPEG.

### Camera Control

- `POST /v1/camera/start`
  Queues a camera start command to the capture pipeline.
- `POST /v1/camera/stop`
  Queues a camera stop command to the capture pipeline.

### WebRTC Signaling

- `WS /ws/webrtc`
  Primary signaling endpoint for the IPC-backed camera stream.
- `WS /ws/webrtc-testsrc`
  Test signaling endpoint that streams an internal generated test pattern instead of IPC camera frames.

The signaling channel exchanges:

- status messages
- SDP offer/answer messages
- ICE candidate messages

## How To Run

Use two terminals.

### Terminal 1: Start The Capture Pipeline

```bash
source .venv/bin/activate
python3 -m capture_pipeline.run_capture_pipeline
```

### Terminal 2: Start The API

```bash
source .venv/bin/activate
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Then open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/docs`

If you are accessing the server from another machine on the network, replace `127.0.0.1` with the host IP.

## Startup Behavior

- The API can start before the capture pipeline.
- If the capture pipeline is not running, snapshot and signaling requests return `503` or a signaling error.
- When the capture pipeline starts later, the API retries IPC attachment on demand.

## Cleanup

If the capture process crashes and leaves shared-memory resources behind:

```bash
python3 -m ipc.cleanup_ipc_script
```

## Notes And Limitations

- The current pipeline is designed around 8-bit frames in shared memory.
- `depth` is not preserved as raw 16-bit depth through IPC.
- The browser page includes both an on-page event log and browser-console WebRTC debug output.
- The capture pipeline may fall back to dummy frames if RealSense startup fails.

## License

See [LICENSE](./LICENSE).
