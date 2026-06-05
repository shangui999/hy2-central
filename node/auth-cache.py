#!/usr/bin/env python3
"""
Hysteria 2 Auth Cache Proxy
- 127.0.0.1:5580 receives auth requests from hy2
- Cache hit: return immediately (~1ms)
- Cache miss: forward to remote central auth
- Remote down: fallback to stale cache
"""

import json
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError

REMOTE_AUTH = "https://your-auth-domain.com/auth"  # CF Tunnel URL
LISTEN_PORT = 5580
CACHE_TTL = 600
CACHE_TTL_FAIL = 600  # 1 hour

NODE_IP = "0.0.0.0"     # this node's public IPv4, set during setup
AUTH_KEY = "CHANGE_ME"   # must match central auth-server.py

_cache = {}  # password -> {"ok": bool, "ts": float}
_lock = threading.Lock()


class CacheHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {args[0]}\n")

    def do_POST(self):
        if self.path != "/auth":
            self._respond(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        password = body.get("auth", "")
        addr = body.get("addr", "unknown")

        with _lock:
            cached = _cache.get(password)
            ttl = CACHE_TTL if cached["ok"] else CACHE_TTL_FAIL
            if cached and time.time() - cached["ts"] < ttl:
                self._respond(200, {"ok": cached["ok"]})
                sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                                 f"CACHE HIT: ok={cached['ok']} from {addr}\n")
                return

        try:
            req = Request(REMOTE_AUTH, data=json.dumps(body).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", "hy2-auth-cache/1.0")
            req.add_header("X-Node", NODE_IP)
            req.add_header("X-Auth-Key", AUTH_KEY)
            with urlopen(req, timeout=5) as resp:
                remote_result = json.loads(resp.read())
            ok = remote_result.get("ok", False)
            with _lock:
                _cache[password] = {"ok": ok, "ts": time.time()}
            tag = "MISS->REMOTE"
        except Exception:
            with _lock:
                stale = _cache.get(password)
            if stale:
                ok = stale["ok"]
                tag = "STALE"
            else:
                ok = False
                tag = "FAIL"

        self._respond(200, {"ok": ok})
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                         f"CACHE {tag}: ok={ok} from {addr}\n")

    def do_GET(self):
        if self.path == "/status":
            with _lock:
                now = time.time()
                active = sum(1 for v in _cache.values() if now - v["ts"] < CACHE_TTL)
                total = len(_cache)
            self._respond(200, {"cached": total, "active": active, "ttl": CACHE_TTL})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = HTTPServer(("127.0.0.1", LISTEN_PORT), CacheHandler)
    print(f"Auth Cache Proxy on 127.0.0.1:{LISTEN_PORT}")
    print(f"Remote: {REMOTE_AUTH}")
    print(f"TTL: {CACHE_TTL}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
