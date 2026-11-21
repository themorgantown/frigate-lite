#!/usr/bin/env python3
"""Tiny fake Frigate for developing Frigate Lite without a real server.

Serves the endpoints Frigate Lite uses (/api/config, /api/<camera>/latest.jpg,
/api/login, /api/events and each event's thumbnail.jpg, snapshot.jpg and clip.mp4)
with moving ffmpeg test patterns, and prints the request rate every
10 s so you can check FPS limits and pausing.

    python3 tools/mock_frigate.py                    # http://127.0.0.1:5055, 12 cameras
    python3 tools/mock_frigate.py --auth cat:meow    # require login like port 8971
    python3 tools/mock_frigate.py --tls              # self-signed HTTPS

Needs ffmpeg on PATH. Uses only the Python standard library.
"""
import argparse
import collections
import glob
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "mock-jwt-token"
NAMES = ["living_room", "kitchen", "hallway", "bedroom", "office", "balcony",
         "cat_tree", "front_door", "bathroom", "litter_box", "window_sill", "storage"]
PORTRAIT = {"hallway"}     # 9:16 camera, to test mixed aspect ratios
FOUR_THREE = {"kitchen"}   # 4:3 camera
OFFLINE = {"storage"}      # answers with X-Frigate-Offline: true
NOT_ON_DASHBOARD = {"bathroom"}


def size_for(name):
    if name in PORTRAIT:
        return 720, 1280
    if name in FOUR_THREE:
        return 960, 720
    return 1280, 720


def render_frames(workdir, names):
    frames = {}
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    has_drawtext = " drawtext " in filters
    for i, name in enumerate(names):
        w, h = size_for(name)
        out = os.path.join(workdir, f"{name}_%02d.jpg")
        base = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", f"testsrc2=size={w}x{h}:rate=5:duration=2"]
        vf = f"hue=h={i * 30}"
        if has_drawtext:
            vf += f",drawtext=text='{name}':fontsize=56:fontcolor=white:x=40:y=h-100:box=1:boxcolor=black@0.6"
        subprocess.run(base + ["-vf", vf, "-q:v", "6", out], check=True)
        files = sorted(glob.glob(os.path.join(workdir, f"{name}_*.jpg")))
        frames[name] = [open(f, "rb").read() for f in files]
    return frames


def render_clips(workdir, names):
    """One short H.264 clip per camera, served as every event clip of that camera."""
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    codec = "libx264" if " libx264 " in encoders else "h264_videotoolbox"
    clips = {}
    for i, name in enumerate(names):
        out = os.path.join(workdir, f"{name}.mp4")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "testsrc2=size=640x360:rate=15:duration=6", "-vf", f"hue=h={i * 30}",
                        "-c:v", codec, "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], check=True)
        clips[name] = open(out, "rb").read()
    return clips


def build_events(names, count=40):
    """Newest first, like Frigate. The newest is still in progress; every 5th has no clip."""
    now = time.time()
    labels = ["person", "cat", "car", "dog", "cat"]
    events = []
    for i in range(count):
        start = now - 60 - i * 1500 - (i * 137) % 600
        events.append({
            "id": f"{start:.6f}-mock{i:02d}",
            "camera": names[i % len(names)],
            "label": labels[i % len(labels)],
            "sub_label": ["Dan", 0.91] if i % 7 == 3 else None,
            "start_time": start,
            "end_time": None if i == 0 else start + 5 + (i * 11) % 90,
            "has_clip": i % 5 != 4,
            "has_snapshot": True,
            "zones": ["porch"] if i % 3 == 0 else [],
            "data": {"top_score": 0.6 + (i % 4) / 10, "score": 0.7},
        })
    return events


def build_config(names):
    cams = {}
    for i, name in enumerate(names):
        w, h = size_for(name)
        cams[name] = {
            "enabled": True,
            "friendly_name": "Cat Tree 🐈" if name == "cat_tree" else None,
            "ui": {"order": i, "dashboard": name not in NOT_ON_DASHBOARD},
            "detect": {"width": w, "height": h, "fps": 5, "enabled": True},
            "ffmpeg": {"inputs": []},
        }
    return {
        "version": "0.17.0-mock",
        "cameras": cams,
        "camera_groups": {
            "cat_spots": {"cameras": ["cat_tree", "window_sill", "litter_box", "living_room"], "order": 1, "icon": "LuCat"},
        },
        "birdseye": {"enabled": True, "restream": True, "width": 1280, "height": 720, "mode": "objects"},
        "auth": {"enabled": False},
    }


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.count = collections.Counter()
        self.heights = collections.defaultdict(set)

    def hit(self, cam, height):
        with self.lock:
            self.count[cam] += 1
            if height:
                self.heights[cam].add(height)

    def report_forever(self, interval=10):
        while True:
            time.sleep(interval)
            with self.lock:
                total = sum(self.count.values())
                cams = len(self.count)
                heights = sorted({h for hs in self.heights.values() for h in hs})
                self.count.clear()
                self.heights.clear()
            per_cam = total / interval / cams if cams else 0
            print(f"[{time.strftime('%H:%M:%S')}] frames: {total / interval:.2f} req/s total, "
                  f"{cams} cameras, {per_cam:.2f} req/s per camera, heights requested: {heights}",
                  flush=True)


def make_handler(frames, clips, events, config, auth, stats):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send(self, status, body=b"", ctype="application/json", headers=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if not auth:
                return True
            cookie = self.headers.get("Cookie", "")
            bearer = self.headers.get("Authorization", "")
            return f"frigate_token={TOKEN}" in cookie or bearer == f"Bearer {TOKEN}"

        def do_POST(self):
            if urlparse(self.path).path != "/api/login":
                return self.send(404, b'{"message":"not found"}')
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not auth:
                return self.send(404, b'{"message":"Authentication is disabled"}')
            try:
                data = json.loads(body)
            except ValueError:
                return self.send(400, b'{"message":"bad json"}')
            if (data.get("user"), data.get("password")) == auth:
                print("login ok", flush=True)
                return self.send(200, b"", "text/plain",
                                 {"Set-Cookie": f"frigate_token={TOKEN}; Path=/; HttpOnly; SameSite=Lax"})
            print("login rejected", flush=True)
            return self.send(401, b'{"message":"Login failed"}')

        def do_GET(self):
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/")
            if url.path == "/api/version":
                return self.send(200, b"0.17.0-mock", "text/plain")
            if not self.authorized():
                return self.send(401, b'{"message":"Unauthorized"}')
            if url.path == "/api/config":
                return self.send(200, json.dumps(config).encode())
            if url.path == "/api/events":
                q = parse_qs(url.query)
                cams = q.get("cameras", ["all"])[0]
                limit = int(q.get("limit", ["100"])[0])
                found = [e for e in events if cams == "all" or e["camera"] in cams.split(",")]
                return self.send(200, json.dumps(found[:limit]).encode())
            if len(parts) == 4 and parts[:2] == ["api", "events"]:
                event = next((e for e in events if e["id"] == parts[2]), None)
                if event is None:
                    return self.send(404, b'{"message":"Event not found"}')
                if parts[3] in ("thumbnail.jpg", "snapshot.jpg"):
                    return self.send(200, frames[event["camera"]][0], "image/jpeg")
                if parts[3] == "clip.mp4" and event["has_clip"]:
                    return self.send(200, clips[event["camera"]], "video/mp4")
                return self.send(404, b'{"message":"No clip"}')
            if len(parts) == 3 and parts[0] == "api" and parts[2] in ("latest.jpg", "latest.webp"):
                cam = parts[1]
                source = frames.get(cam) or (frames[NAMES[0]] if cam == "birdseye" else None)
                if source is None:
                    return self.send(404, b'{"message":"Camera not found"}')
                q = parse_qs(url.query)
                stats.hit(cam, int(q.get("height", q.get("h", ["0"]))[0]))
                frame = source[int(time.time() * 5) % len(source)]
                headers = {"X-Frigate-Offline": "true"} if cam in OFFLINE else {}
                return self.send(200, frame, "image/jpeg", headers)
            return self.send(404, b'{"message":"not found"}')

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5055)
    ap.add_argument("--cameras", type=int, default=12, help=f"1-{len(NAMES)}")
    ap.add_argument("--auth", metavar="USER:PASS", help="require /api/login like Frigate's port 8971")
    ap.add_argument("--tls", action="store_true", help="serve HTTPS with a throwaway self-signed certificate")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")

    names = NAMES[: max(1, min(args.cameras, len(NAMES)))]
    workdir = tempfile.mkdtemp(prefix="mock-frigate-")
    print(f"rendering test frames for {len(names)} cameras…", flush=True)
    frames = render_frames(workdir, names)
    clips = render_clips(workdir, names)
    auth = tuple(args.auth.split(":", 1)) if args.auth else None
    stats = Stats()
    threading.Thread(target=stats.report_forever, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(frames, clips, build_events(names), build_config(names), auth, stats))
    scheme = "http"
    if args.tls:
        cert, key = os.path.join(workdir, "cert.pem"), os.path.join(workdir, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-keyout", key, "-out", cert],
                       check=True, capture_output=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"mock Frigate on {scheme}://127.0.0.1:{args.port}" + (f" (login {args.auth})" if auth else ""), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
