# hy2-central

Hysteria 2 集中式用户管理与多节点部署工具。

## 架构

```
┌──────────────────────────────────────────────┐
│  Central Server (长期稳定机器)                 │
│                                              │
│  auth-server.py (127.0.0.1:5580 + :5581)     │
│  ├─ :5580 /auth  ← CF Tunnel 暴露 (HTTPS)    │
│  └─ :5581 /users ← 本地管理 API               │
│                                              │
│  hy2u   ← CLI 用户/节点管理工具               │
│  users.json  ← 用户数据                      │
│  nodes.json  ← 节点注册表                     │
└──────────────┬───────────────────────────────┘
               │ https://your-auth-domain.com/auth
               │ (X-Auth-Key 校验)
    ┌──────────┴──────────────────────┐
    │        Hy2 Node (VPS)           │
    │                                 │
    │  hysteria-server (:9443 UDP)    │
    │  auth-cache.py (127.0.0.1:5580) │
    │  ├─ HIT  → 直接返回 (~1ms)      │
    │  ├─ MISS → 转发 Central (~50ms) │
    │  └─ FAIL → 过期缓存兜底          │
    │                                 │
    │  iptables 端口跳跃 50000-60000   │
    └─────────────────────────────────┘
```

## 特性

- **集中认证** — 用户数据只维护一份，所有节点共享
- **本地缓存** — 每个节点缓存认证结果 (默认 1 小时 TTL)，Central 不可用时已缓存用户照常连接
- **共享密钥** — `X-Auth-Key` 保护 auth 端点，域名泄露也无法利用
- **节点追踪** — 日志记录 用户 + 客户端 IP + 来源节点
- **一键部署** — `setup-node.sh` 自动完成新节点全套配置
- **CLI 管理** — `hy2u` 增删用户 / 续期 / 改密码 / 生成 URI / 管理节点

## 目录结构

```
central/
  auth-server.py      # 认证服务 (部署到 Central Server)
  hy2u                 # 用户/节点管理 CLI (部署到 Central Server)
node/
  auth-cache.py        # 认证缓存代理 (部署到每个 Hy2 Node)
  setup-node.sh        # 一键节点部署脚本
systemd/
  hy2-auth.service           # Central: 认证服务
  hy2-auth-cache.service     # Node: 缓存代理
  hysteria-server.service    # Node: Hysteria 2 服务
examples/
  config.yaml          # Hysteria 2 节点配置示例
  nodes.json           # 节点注册表示例
  users.json           # 用户数据示例
```

## 部署

### 1. Central Server

```bash
# 复制文件
cp central/auth-server.py /etc/hysteria/
cp central/hy2u /usr/local/bin/
cp systemd/hy2-auth.service /etc/systemd/system/

# 修改配置
# 编辑 auth-server.py 中的 AUTH_KEY (建议: openssl rand -hex 16)

# 启动
systemctl daemon-reload
systemctl enable --now hy2-auth

# 通过 CF Tunnel 暴露 5580 端口
# cloudflared tunnel ... --url http://localhost:5580
```

### 2. Hy2 Node (一键部署)

**前提**: 你已经有一台新 VPS (root 权限, Debian/Ubuntu)，想把它加为 hy2 节点。

**Step 1** — SSH 到新 VPS 上，下载并执行部署脚本:

```bash
# 方式 A: 从 Central Server 拉取 (推荐，脚本里已包含你的 AUTH_KEY 等配置)
ssh your-central 'cat /etc/hysteria/setup-node.sh' | bash -s -- us-lax-01
#                                                                ^^^^^^^^
#                                                                节点 tag，随便起

# 方式 B: 从 GitHub 下载 (需要手动改配置)
curl -fsSL https://raw.githubusercontent.com/shangui999/hy2-central/main/node/setup-node.sh -o setup-node.sh
# 编辑 setup-node.sh，修改 REMOTE_AUTH 和 AUTH_KEY
vim setup-node.sh
bash setup-node.sh us-lax-01
```

脚本自动完成 7 步:
1. 开启 BBR + 调大 UDP buffer
2. 安装 Hysteria 2
3. 生成自签证书 (CN=bing.com, 100 年)
4. 写入 config.yaml (auth 指向本地缓存代理)
5. 配置端口跳跃 iptables (IPv4 + IPv6, 50000-60000 → 9443)
6. 部署认证缓存代理 (auth-cache.py)
7. 启动 hysteria-server + auth-cache

**Step 2** — 脚本跑完后会输出节点信息和一条注册命令:

```
===============================
  部署完成！
===============================

  IP:   1.2.3.4
  IPv6: 2001:db8::1
  Port: 9443
  Hop:  50000-60000
  PIN:  ABCDEF1234567890...
  Tag:  us-lax-01

─── 在 Central 上执行以下命令注册节点 ───

ssh your-central 'hy2u add-node --tag us-lax-01 --ip 1.2.3.4 --pin ABCDEF... --mport 50000-60000'
```

**Step 3** — 复制最后那条命令，在你本地终端执行，节点就注册到 Central 了:

```bash
# 直接粘贴执行
ssh your-central 'hy2u add-node --tag us-lax-01 --ip 1.2.3.4 --pin ABCDEF... --mport 50000-60000'
# ✓ 节点已注册: us-lax-01 (1.2.3.4:9443)
```

完成。新节点立刻可用，现有用户无需任何改动即可连接新节点。

### 3. 手动部署 Node

```bash
# 复制文件
cp node/auth-cache.py /etc/hysteria/
cp examples/config.yaml /etc/hysteria/
cp systemd/hy2-auth-cache.service /etc/systemd/system/
cp systemd/hysteria-server.service /etc/systemd/system/

# 修改 auth-cache.py 中的:
#   REMOTE_AUTH = "https://your-auth-domain.com/auth"
#   X-Node = "节点 IP"
#   X-Auth-Key = "你的密钥"

# 生成自签证书
openssl ecparam -genkey -name prime256v1 -out /etc/hysteria/server.key
openssl req -new -x509 -key /etc/hysteria/server.key \
    -out /etc/hysteria/server.crt -days 36500 -subj "/CN=bing.com"

# 端口跳跃
iptables -t nat -A PREROUTING -p udp --dport 50000:60000 -j REDIRECT --to-ports 9443
ip6tables -t nat -A PREROUTING -p udp --dport 50000:60000 -j REDIRECT --to-ports 9443
netfilter-persistent save

# 启动
systemctl daemon-reload
systemctl enable --now hy2-auth-cache hysteria-server
```

## 用户管理 (hy2u)

```bash
# 用户操作
hy2u list                          # 查看所有用户
hy2u add alice "" 3                # 随机密码, 3个月有效期
hy2u add bob mypass 0              # 指定密码, 永不过期
hy2u del alice                     # 删除用户
hy2u renew alice 6                 # 续期 6 个月
hy2u passwd alice                  # 随机新密码
hy2u uri alice                     # 输出所有节点的客户端 URI

# 节点操作
hy2u nodes                         # 查看节点列表
hy2u add-node --tag us-lax --ip 1.2.3.4 --pin ABC123 --mport 50000-60000
hy2u del-node us-lax               # 删除节点
```

## 配置项

| 文件 | 变量 | 说明 | 默认值 |
|------|------|------|--------|
| auth-server.py | `AUTH_KEY` | 共享密钥，防止未授权访问 | 需自行生成 |
| auth-server.py | `AUTH_PORT` | /auth 端口 (CF Tunnel 暴露) | 5580 |
| auth-server.py | `MGMT_PORT` | 管理 API 端口 (仅本地) | 5581 |
| auth-cache.py | `REMOTE_AUTH` | Central auth URL | 需配置 |
| auth-cache.py | `CACHE_TTL` | 缓存过期时间 (秒) | 3600 |
| setup-node.sh | `AUTH_KEY` | 同 auth-server.py | 需配置 |
| setup-node.sh | `REMOTE_AUTH` | 同 auth-cache.py | 需配置 |

## 安全

- Auth 端点通过 Cloudflare Tunnel 暴露，自带 DDoS 防护
- `X-Auth-Key` 共享密钥校验，无密钥请求直接 403
- 管理 API 仅监听 127.0.0.1，只能通过 SSH 访问
- 缓存代理不缓存管理操作，仅缓存认证结果
- 日志记录所有认证事件 (成功/失败/拒绝) 及来源节点

## License

MIT
