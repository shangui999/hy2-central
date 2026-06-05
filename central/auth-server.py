#!/usr/bin/env python3
"""
Hysteria 2 HTTP Auth Server (dual-port, centralized)
- Port 5580 (127.0.0.1): /auth only — exposed via CF Tunnel
- Port 5581 (127.0.0.1): full management API — local SSH only
"""

import json
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

DATA_FILE = "/etc/hysteria/users.json"
AUTH_KEY = "CHANGE_ME"  # openssl rand -hex 16
AUTH_PORT = 5580
MGMT_PORT = 5581

_lock = threading.Lock()


def load_users():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_users(users):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def is_expired(user_info):
    expire_at = user_info.get("expire_at")
    if not expire_at:
        return False
    try:
        expire_time = datetime.fromisoformat(expire_at)
        if expire_time.tzinfo is None:
            expire_time = expire_time.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expire_time
    except (ValueError, TypeError):
        return False


class AuthOnlyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [auth] {args[0]}\n")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/auth":
            self._handle_auth()
        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def _handle_auth(self):
        key = self.headers.get("X-Auth-Key", "")
        if key != AUTH_KEY:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sys.stderr.write(f"[{ts}] REJECTED (bad key) from {self.client_address[0]}\n")
            self._respond(403, {"error": "forbidden"})
            return
        body = self._read_body()
        password = body.get("auth", "")
        client_addr = body.get("addr", "unknown")
        node = self.headers.get("X-Node", "unknown")
        with _lock:
            users = load_users()
        for username, info in users.items():
            if info.get("password") == password:
                if is_expired(info):
                    sys.stderr.write(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"AUTH DENIED (expired): {username} from {client_addr} via {node}\n"
                    )
                    self._respond(200, {"ok": False})
                    return
                sys.stderr.write(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"AUTH OK: {username} from {client_addr} via {node}\n"
                )
                self._respond(200, {"ok": True})
                return
        sys.stderr.write(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"AUTH DENIED (no match): from {client_addr} via {node}\n"
        )
        self._respond(200, {"ok": False})


class MgmtHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [mgmt] {args[0]}\n")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/users":
            self._handle_list_users()
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/users":
            self._handle_add_user()
        else:
            self._respond(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/users/"):
            self._handle_delete_user(self.path[len("/users/"):])
        else:
            self._respond(404, {"error": "not found"})

    def do_PUT(self):
        if self.path.startswith("/users/"):
            self._handle_update_user(self.path[len("/users/"):])
        else:
            self._respond(404, {"error": "not found"})

    def _handle_add_user(self):
        body = self._read_body()
        username = body.get("username", "").strip()
        password = body.get("password", "").strip()
        expire_at = body.get("expire_at")
        if not username or not password:
            self._respond(400, {"error": "username and password required"})
            return
        with _lock:
            users = load_users()
            if username in users:
                self._respond(409, {"error": f"user '{username}' already exists"})
                return
            users[username] = {
                "password": password,
                "expire_at": expire_at,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            save_users(users)
        self._respond(201, {"ok": True, "username": username, "expire_at": expire_at})

    def _handle_delete_user(self, username):
        with _lock:
            users = load_users()
            if username not in users:
                self._respond(404, {"error": f"user '{username}' not found"})
                return
            del users[username]
            save_users(users)
        self._respond(200, {"ok": True, "deleted": username})

    def _handle_update_user(self, username):
        body = self._read_body()
        with _lock:
            users = load_users()
            if username not in users:
                self._respond(404, {"error": f"user '{username}' not found"})
                return
            if "password" in body:
                users[username]["password"] = body["password"]
            if "expire_at" in body:
                users[username]["expire_at"] = body["expire_at"]
            save_users(users)
        self._respond(200, {"ok": True, "user": users[username]})

    def _handle_list_users(self):
        with _lock:
            users = load_users()
        result = []
        for username, info in users.items():
            expired = is_expired(info)
            result.append({
                "username": username,
                "expire_at": info.get("expire_at"),
                "created_at": info.get("created_at"),
                "status": "expired" if expired else "active",
            })
        self._respond(200, {"users": result, "total": len(result)})


def main():
    if not os.path.exists(DATA_FILE):
        save_users({})

    auth_server = HTTPServer(("127.0.0.1", AUTH_PORT), AuthOnlyHandler)
    mgmt_server = HTTPServer(("127.0.0.1", MGMT_PORT), MgmtHandler)

    print(f"Auth endpoint on 127.0.0.1:{AUTH_PORT} (for CF Tunnel)")
    print(f"Mgmt endpoint on 127.0.0.1:{MGMT_PORT} (local only)")

    auth_thread = threading.Thread(target=auth_server.serve_forever, daemon=True)
    mgmt_thread = threading.Thread(target=mgmt_server.serve_forever, daemon=True)
    auth_thread.start()
    mgmt_thread.start()

    try:
        auth_thread.join()
    except KeyboardInterrupt:
        print("\nShutting down...")
        auth_server.shutdown()
        mgmt_server.shutdown()


if __name__ == "__main__":
    main()
