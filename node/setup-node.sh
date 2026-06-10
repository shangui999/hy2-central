#!/bin/bash
set -e

AUTH_URL="http://127.0.0.1:5580/auth"
REMOTE_AUTH="https://your-auth-domain.com/auth"
LISTEN_PORT=9443
HOP_RANGE="50000:60000"
SNI="bing.com"
MASQ_URL="https://bing.com"
AUTH_KEY="CHANGE_ME"

# ─── 交互：节点 tag ───
TAG="${1:-}"
if [ -z "$TAG" ]; then
    read -rp "节点 tag (如 us-lax-01): " TAG
    [ -z "$TAG" ] && echo "tag 不能为空" && exit 1
fi

echo "==============================="
echo "  Hysteria 2 节点一键部署"
echo "  tag: $TAG"
echo "==============================="
echo ""

# ─── 1. 系统优化 ───
echo "[1/7] 系统优化 (BBR + UDP buffer)..."
grep -q 'net.core.default_qdisc=fq' /etc/sysctl.conf 2>/dev/null || echo 'net.core.default_qdisc=fq' >> /etc/sysctl.conf
grep -q 'net.ipv4.tcp_congestion_control=bbr' /etc/sysctl.conf 2>/dev/null || echo 'net.ipv4.tcp_congestion_control=bbr' >> /etc/sysctl.conf
grep -q 'net.core.rmem_max=16777216' /etc/sysctl.conf 2>/dev/null || echo 'net.core.rmem_max=16777216' >> /etc/sysctl.conf
grep -q 'net.core.wmem_max=16777216' /etc/sysctl.conf 2>/dev/null || echo 'net.core.wmem_max=16777216' >> /etc/sysctl.conf
sysctl -p >/dev/null 2>&1
echo "  BBR: $(sysctl -n net.ipv4.tcp_congestion_control)"

# ─── 2. 安装 hysteria ───
echo "[2/7] 安装 Hysteria 2..."
if [ -f /usr/local/bin/hysteria ]; then
    echo "  已安装: $(/usr/local/bin/hysteria version 2>&1 | grep Version | awk '{print $2}')"
else
    curl -fsSL https://get.hy2.sh/ | bash
fi

# ─── 3. 自签证书 ───
echo "[3/7] 生成自签证书 (CN=$SNI, 100年)..."
id hysteria &>/dev/null || useradd -r -s /usr/sbin/nologin hysteria
mkdir -p /etc/hysteria
openssl ecparam -genkey -name prime256v1 -out /etc/hysteria/server.key 2>/dev/null
openssl req -new -x509 -key /etc/hysteria/server.key \
    -out /etc/hysteria/server.crt \
    -days 36500 -subj "/CN=$SNI" 2>/dev/null
chown hysteria:root /etc/hysteria/server.crt /etc/hysteria/server.key
chmod 644 /etc/hysteria/server.crt
chmod 600 /etc/hysteria/server.key
echo "  证书已生成"

# ─── 4. 配置文件 ───
echo "[4/7] 写入配置..."
cat > /etc/hysteria/config.yaml << CONF
listen: :$LISTEN_PORT

tls:
  cert: /etc/hysteria/server.crt
  key: /etc/hysteria/server.key

auth:
  type: http
  http:
    url: $AUTH_URL
    insecure: false

bandwidth:
  up: 100 mbps
  down: 100 mbps

masquerade:
  type: proxy
  proxy:
    url: $MASQ_URL
    rewriteHost: true
CONF
chown hysteria:root /etc/hysteria/config.yaml

# systemd
cat > /etc/systemd/system/hysteria-server.service << SVC
[Unit]
Description=Hysteria Server Service (config.yaml)
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/hysteria server --config /etc/hysteria/config.yaml
WorkingDirectory=~
User=hysteria
Group=hysteria
Environment=HYSTERIA_LOG_LEVEL=info
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload
echo "  配置已写入"

# ─── 5. 端口跳跃 ───
echo "[5/7] 配置端口跳跃 (UDP $HOP_RANGE → $LISTEN_PORT)..."
apt-get install -y iptables iptables-persistent </dev/null >/dev/null 2>&1 || true
if ! iptables -t nat -C PREROUTING -p udp --dport $HOP_RANGE -j REDIRECT --to-ports $LISTEN_PORT 2>/dev/null; then
    iptables -t nat -A PREROUTING -p udp --dport $HOP_RANGE -j REDIRECT --to-ports $LISTEN_PORT
fi
if ! ip6tables -t nat -C PREROUTING -p udp --dport $HOP_RANGE -j REDIRECT --to-ports $LISTEN_PORT 2>/dev/null; then
    ip6tables -t nat -A PREROUTING -p udp --dport $HOP_RANGE -j REDIRECT --to-ports $LISTEN_PORT
fi
netfilter-persistent save >/dev/null 2>&1 || true
echo "  iptables 规则已持久化"

# ─── 6. 启动服务 ───

# ─── 6. Auth 缓存代理 ───
echo "[6/7] 部署 Auth 缓存代理..."
cat > /etc/hysteria/auth-cache.py << 'CACHEPY'
#!/usr/bin/env python3
import json, sys, time, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError

REMOTE_AUTH = "REMOTE_AUTH_PLACEHOLDER"
LISTEN_PORT = 5580
CACHE_TTL = 600
CACHE_TTL_FAIL = 600

_cache = {}
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
            if cached:
                ttl = CACHE_TTL if cached.get("ok") else CACHE_TTL_FAIL
            if cached and time.time() - cached["ts"] < ttl:
                self._respond(200, {"ok": cached["ok"]})
                sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] CACHE HIT: ok={cached['ok']} from {addr}\n")
                return
        try:
            req = Request(REMOTE_AUTH, data=json.dumps(body).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", "hy2-auth-cache/1.0")
            req.add_header("X-Node", "NODE_IP_PLACEHOLDER")
            req.add_header("X-Auth-Key", "AUTH_KEY_PLACEHOLDER")
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
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] CACHE {tag}: ok={ok} from {addr}\n")

    def do_GET(self):
        if self.path == "/status":
            with _lock:
                now = time.time()
                active = sum(1 for v in _cache.values() if now - v["ts"] < CACHE_TTL)
            self._respond(200, {"cached": len(_cache), "active": active, "ttl": CACHE_TTL})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", LISTEN_PORT), CacheHandler)
    print(f"Auth Cache on 127.0.0.1:{LISTEN_PORT} -> {REMOTE_AUTH}, TTL={CACHE_TTL}s")
    server.serve_forever()
CACHEPY
sed -i "s|REMOTE_AUTH_PLACEHOLDER|$REMOTE_AUTH|g" /etc/hysteria/auth-cache.py
sed -i "s|NODE_IP_PLACEHOLDER|$SERVER_IP|g" /etc/hysteria/auth-cache.py
sed -i "s|AUTH_KEY_PLACEHOLDER|$AUTH_KEY|g" /etc/hysteria/auth-cache.py
chmod +x /etc/hysteria/auth-cache.py

cat > /etc/systemd/system/hy2-auth-cache.service << 'CACHESVC'
[Unit]
Description=Hysteria 2 Auth Cache Proxy
After=network.target
Before=hysteria-server.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /etc/hysteria/auth-cache.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
CACHESVC
systemctl daemon-reload
systemctl enable --now hy2-auth-cache >/dev/null 2>&1
echo "  auth-cache: running ✓"

echo "[7/7] 启动服务..."
systemctl enable --now hysteria-server >/dev/null 2>&1
sleep 2
if systemctl is-active --quiet hysteria-server; then
    echo "  hysteria-server: running ✓"
else
    echo "  hysteria-server: FAILED ✗"
    systemctl status hysteria-server --no-pager | tail -5
    exit 1
fi

# ─── 提取节点信息 ───
SERVER_IP=$(curl -s --max-time 5 ipinfo.io/ip 2>/dev/null || curl -s --max-time 5 ifconfig.me 2>/dev/null)
SERVER_IPV6=$(ip -6 addr show scope global 2>/dev/null | grep inet6 | awk '{print $2}' | cut -d/ -f1 | head -1)
PIN=$(openssl x509 -in /etc/hysteria/server.crt -outform DER 2>/dev/null | openssl dgst -sha256 -hex 2>/dev/null | awk '{print $NF}' | tr 'a-f' 'A-F')

echo ""
echo "==============================="
echo "  部署完成！"
echo "==============================="
echo ""
echo "  IP:   $SERVER_IP"
[ -n "$SERVER_IPV6" ] && echo "  IPv6: $SERVER_IPV6"
echo "  Port: $LISTEN_PORT"
echo "  Hop:  50000-60000"
echo "  PIN:  $PIN"
echo "  Tag:  $TAG"
echo ""
echo "─── 在 ali-hk 上执行以下命令注册节点 ───"
echo ""
CMD="ssh ali-hk 'hy2u add-node --tag $TAG --ip $SERVER_IP --pin $PIN --mport 50000-60000"
if [ -n "$SERVER_IPV6" ]; then
    CMD="$CMD --ipv6 $SERVER_IPV6"
fi
CMD="$CMD'"
echo "$CMD"
