from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Check FastAPI HTTP endpoints only.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL.")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    try:
        status_data = fetch_json(f"{base_url}/v1/status")
        print(f"status: {status_data}")
    except urllib.error.URLError as exc:
        print(f"FAIL: could not reach /v1/status: {exc}")
        return 1

    try:
        with urllib.request.urlopen(f"{base_url}/v1/frame.jpg", timeout=5) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
        print(f"frame.jpg: content_type={content_type} bytes={len(body)}")
    except urllib.error.HTTPError as exc:
        print(f"FAIL: /v1/frame.jpg returned HTTP {exc.code}")
        return 1
    except urllib.error.URLError as exc:
        print(f"FAIL: could not reach /v1/frame.jpg: {exc}")
        return 1

    if not status_data.get("ok"):
        print("FAIL: /v1/status did not report ready.")
        return 1
    if "image/jpeg" not in content_type.lower():
        print("FAIL: /v1/frame.jpg did not return image/jpeg.")
        return 1
    if not body:
        print("FAIL: /v1/frame.jpg returned an empty body.")
        return 1

    print("PASS: API HTTP checks succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
