import asyncio
import json
import os
import cv2
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

SIGNAL_URL = os.environ.get("SIGNAL_URL", "ws://127.0.0.1:8000/ws/webrtc")
# set SIGNAL_URL=ws://127.0.0.1:8000/ws/webrtc-testsrc to compare against testsrc

async def run():
    print("Using SIGNAL_URL:", SIGNAL_URL)
    pc = RTCPeerConnection()
    remote_set = asyncio.Event()
    got_frame = asyncio.Event()
    pending_remote_ice = []

    # Important: explicitly request receiving video
    pc.addTransceiver("video", direction="recvonly")

    @pc.on("connectionstatechange")
    async def _():
        print("PC connectionState:", pc.connectionState)

    @pc.on("iceconnectionstatechange")
    async def _():
        print("PC iceConnectionState:", pc.iceConnectionState)

    @pc.on("icegatheringstatechange")
    async def _():
        print("PC iceGatheringState:", pc.iceGatheringState)

    @pc.on("signalingstatechange")
    async def _():
        print("PC signalingState:", pc.signalingState)

    @pc.on("track")
    def on_track(track):
        print("Track received:", track.kind)

        @track.on("ended")
        async def _():
            print("Track ended:", track.kind)

        async def recv_video():
            n = 0
            try:
                while True:
                    frame = await track.recv()
                    img = frame.to_ndarray(format="bgr24")
                    n += 1
                    got_frame.set()
                    print(
                        f"frame={n} size={img.shape[1]}x{img.shape[0]} "
                        f"mean={float(img.mean()):.2f} "
                        f"min={int(img.min())} max={int(img.max())}"
                    )
                    cv2.imshow("WebRTC RX", img)
                    key = cv2.waitKey(1) & 0xFF
                    if key == 27 or key == ord("q"):
                        await pc.close()
                        cv2.destroyAllWindows()
                        return
            except Exception as exc:
                print("recv_video failed:", repr(exc))

        asyncio.create_task(recv_video())

    @pc.on("icecandidate")
    async def on_icecandidate(candidate):
        if candidate is None:
            print("Local ICE complete")
            return
        if ws is None:
            return
        await ws.send(json.dumps({
            "type": "ice-candidate",
            "candidate": candidate.candidate,
            "sdpMLineIndex": candidate.sdpMLineIndex,
        }))
        print("Local ICE sent")

    async def watchdog():
        await asyncio.sleep(10)
        if not got_frame.is_set():
            print("No frames after 10s")
            print("connectionState:", pc.connectionState)
            print("iceConnectionState:", pc.iceConnectionState)
            print("iceGatheringState:", pc.iceGatheringState)
            print("signalingState:", pc.signalingState)

    asyncio.create_task(watchdog())

    ws = None

    async with websockets.connect(SIGNAL_URL, max_size=8 * 1024 * 1024) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("type")
            print("Signal:", t)

            if t == "hello":
                continue

            if t == "status":
                print("Status payload:", msg)
                continue

            if t == "offer":
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["sdp"], type="offer")
                )
                remote_set.set()

                print("=== REMOTE OFFER SDP ===")
                print(msg["sdp"][:2000])

                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                print("=== LOCAL ANSWER SDP ===")
                print(pc.localDescription.sdp[:2000])
                print("Has m=video:", "m=video" in pc.localDescription.sdp)
                print("Has a=recvonly:", "a=recvonly" in pc.localDescription.sdp)
                print("Has candidate:", "a=candidate:" in pc.localDescription.sdp)

                await ws.send(json.dumps({
                    "type": "answer",
                    "sdp": pc.localDescription.sdp,
                }))
                print("Answer sent")

                while pending_remote_ice:
                    ice = pending_remote_ice.pop(0)
                    await pc.addIceCandidate(ice)
                    print("Queued remote ICE added")
                continue

            if t == "ice-candidate":
                ice = candidate_from_sdp(msg["candidate"])
                ice.sdpMLineIndex = msg["sdpMLineIndex"]
                if not remote_set.is_set():
                    pending_remote_ice.append(ice)
                    print("Remote ICE queued")
                    continue
                await pc.addIceCandidate(ice)
                print("Remote ICE added")
                continue

            if t == "error":
                raise RuntimeError(msg.get("message", "Signaling error"))

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
