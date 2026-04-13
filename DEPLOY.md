# Memecoin Screener — Complete Operations Guide

> Everything you need to deploy, access, troubleshoot, and evolve the screener.
> Written from real deployment experience on a k3s + Tailscale + Contabo setup.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Strategy Overview](#strategy-overview)
3. [Alert Types](#alert-types)
4. [Git Workflow and Secret Protection](#git-workflow-and-secret-protection)
5. [First Deployment](#first-deployment)
6. [Accessing the Dashboard](#accessing-the-dashboard)
7. [External Access — Cloudflare Tunnel](#external-access--cloudflare-tunnel)
8. [Dashboard Password Protection](#dashboard-password-protection)
9. [Tuning Without Rebuilding](#tuning-without-rebuilding)
10. [Useful Commands](#useful-commands)
11. [Troubleshooting](#troubleshooting)
12. [Known Issues and Lessons Learned](#known-issues-and-lessons-learned)
13. [Future Roadmap](#future-roadmap)

---

## Architecture

```
Your Phone / Desktop (Tailscale or browser)
        │
        ▼
cp-contabo (k3s control plane)
  ├── Tailscale IP: 100.98.75.49
  ├── SSH port: 60607
  └── port-forward: 8080 → screener dashboard
        │
        ▼
worker-contabo (k3s worker node)
  ├── Tailscale IP: 100.74.235.111
  └── pod: memecoin-screener
       ├── screener container   — scan loop, both strategies, tracker thread
       └── dashboard container  — Flask UI on port 8080

postgres StatefulSet
  └── worker-contabo, local-path storage (k3s built-in)
      /var/lib/rancher/k3s/storage/
```

### Networking

All inter-node traffic routes over **Tailscale (WireGuard)**. No public ports are open.
k3s Flannel is configured with `flannel-iface: tailscale0` so pod-to-pod
traffic across nodes goes through the encrypted tunnel.

Both nodes' nftables firewalls include the k3s pod and service CIDRs:
```
ip saddr 10.42.0.0/16 accept   # pod network
ip saddr 10.43.0.0/16 accept   # service network
```
Without these rules, pods cannot reach CoreDNS and everything crashes.

---


## Strategy Overview

| | Strategy A — Fast | Strategy B — Swing |
|---|---|---|
| Token age | 5min – 24hr | 6hr – 120hr (5 days) |
| Risk profile | Higher | Lower |
| Security filters | Relaxed | Strict |
| Min holders | 50 | 200 |
| Pullback watch window | 12 hours | 48 hours |
| Min score | 0.55 | 0.62 |

Both strategies run simultaneously in the same pod.
Disable either by setting `enabled: false` in `config.yaml` — no rebuild needed.

---

## Alert Types

### 1. Initial Alert
Fires when a token passes all 8 filter layers and composite scoring.
Records MC and price at alert time. Begins pullback monitoring.

### 2. Pullback Entry Alert
Fires when a token drops the right percentage from its peak with volume confirmation.

| | Strategy A | Strategy B |
|---|---|---|
| Min pullback | 15% | 20% |
| Max pullback | 55% | 60% |
| Min 5m volume | $1,000 | $2,000 |
| Watch window | 12 hours | 48 hours |

The alert shows: MC at initial alert → peak MC reached → current MC at pullback.
This is the buy signal — not the initial alert.

### 3. Milestone Alert
Fires at: **2x / 5x / 10x** vs initial alert MC
and: **2x / 3x / 5x** vs pullback entry price

### 4. 72h Outcome
Final result 72 hours after initial alert.
Classified as: moon (5x+) | up (2-5x) | flat (0.8-2x) | down (<0.8x) | dead

---

## Git Workflow and Secret Protection

### Repository

```
GitHub:  git@github.com:philjamesexcel-source/pjmscanner.git
Local:   ~/pjmscanner   (desktop)
VPS:     ~/pjmscanner   (cp-contabo)
```

### The two-file pattern

Every secret-containing YAML has two versions:

| File | Contains | Committed to git |
|---|---|---|
| `k8s.yaml` | `REPLACE_WITH_*` placeholders | ✅ Yes — safe |
| `k8s.local.yaml` | Real credentials | ❌ No — VPS/desktop only |
| `postgres.yaml` | `REPLACE_WITH_*` placeholders | ✅ Yes — safe |
| `postgres.local.yaml` | Real credentials | ❌ No — VPS/desktop only |

`.gitignore` contains `*.local.yaml` so git never sees the local files.

### How check_secrets.sh protects you

`check_secrets.sh` is installed as a **git pre-commit hook** at
`.git/hooks/pre-commit`. Git runs it automatically before recording
any commit. If it finds a secret, it exits with code 1 — git aborts
the commit entirely and nothing gets written to git history.

What it scans:
- Telegram bot token pattern `:[0-9]{10}:AA`
- Helius API key UUID format
- Filled-in Telegram tokens and channel IDs in YAML
- Whether `k8s.yaml` and `postgres.yaml` still contain `REPLACE_WITH_`
  (if the placeholders are gone, you accidentally filled in real values
  and the commit is blocked)

The hook is local only — it lives in `.git/hooks/` which git does not
commit. Install it on every machine you clone to.

### Setting up on a new machine after cloning

```bash
git clone git@github.com:philjamesexcel-source/pjmscanner.git
cd pjmscanner

# Install the pre-commit hook
mkdir -p .git/hooks
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash
bash check_secrets.sh
EOF
chmod +x .git/hooks/pre-commit
chmod +x check_secrets.sh

# Create local secret files
cp k8s.yaml k8s.local.yaml
cp postgres.yaml postgres.local.yaml

# Fill in real credentials
nano k8s.local.yaml
nano postgres.local.yaml
```

### Daily workflow

```bash
# Edit code
nano screener.py

# Stage and commit — hook runs automatically
git add screener.py
git commit -m "fix: adjust volume thresholds"
git push origin main
```

### Pulling updates to the VPS

```bash
# On CP
cd ~/pjmscanner
git pull origin main
```

If only `config.yaml` changed — no rebuild needed:
```bash
sudo kubectl apply -f k8s.local.yaml
sudo kubectl rollout restart deployment/memecoin-screener -n screener
```

If Python files changed — rebuild and push image first:
```bash
# On desktop
docker build -t covenantwealth/memecoin-screener:v2 .
docker push covenantwealth/memecoin-screener:v2

# Update image tag in k8s.local.yaml, then on CP:
sudo kubectl apply -f k8s.local.yaml
sudo kubectl rollout restart deployment/memecoin-screener -n screener
```

---

## First Deployment

### Step 1 — Rotate API keys

Both of these were previously exposed in the old repo. Rotate before deploying:
- Helius API key → helius.dev dashboard → create new key
- Telegram bot token → BotFather → /revoke → /newbot or /token

### Step 2 — Build and push Docker image

```bash
# On desktop
cd ~/pjmscanner
docker build -t covenantwealth/memecoin-screener:v1 .
docker push covenantwealth/memecoin-screener:v1
```

### Step 3 — Fill in secrets on VPS

```bash
# On CP
cd ~/pjmscanner
cp k8s.yaml k8s.local.yaml
cp postgres.yaml postgres.local.yaml
nano k8s.local.yaml         # fill in all REPLACE_WITH_* values
nano postgres.local.yaml    # fill in POSTGRES_PASSWORD (must match k8s.local.yaml)
```

### Step 4 — Deploy PostgreSQL

```bash
sudo kubectl apply -f postgres.local.yaml
sudo kubectl rollout status statefulset/postgres -n screener
```

### Step 5 — Deploy the screener

```bash
sudo kubectl apply -f k8s.local.yaml
sudo kubectl get pods -n screener -w
```

Expected final state:
```
NAME                                 READY   STATUS    RESTARTS   AGE
memecoin-screener-xxxxxxxxx-xxxxx    2/2     Running   0          60s
postgres-0                           1/1     Running   0          3m
```

`2/2` = screener + dashboard both running.

### Step 6 — Verify it's working

```bash
sudo kubectl logs -f deploy/memecoin-screener -c screener -n screener
```

You should see within 30 seconds:
```
DB: connected to PostgreSQL
DB: schema ready
Screener started | Strategies: ['⚡ FAST', '🎯 SWING'] | Interval: 15min
Tracker: background checker started
── Starting scan ──
Discovered N candidate pairs
── Scan done. Alerts: 0 ──
Sleeping 15min…
```

Zero alerts on the first scan is normal. Your Telegram channel should
receive a startup message.

---

## Accessing the Dashboard

### Method 1 — SSH Tunnel (current setup, works now)

Requires two terminals running simultaneously.

**Terminal 1 — on CP** (keep running):
```bash
sudo kubectl port-forward svc/screener-dashboard 8080:8080 -n screener --address 127.0.0.1
```

**Terminal 2 — on desktop** (keep running):
```bash
ssh -N -L 8080:127.0.0.1:8080 phil@100.98.75.49 -p 60607 -i ~/.ssh/contabo
```

Open: `http://localhost:8080`

### Method 2 — Tailscale (any device on your tailnet)

Same two terminals as above, but access from any Tailscale device:
```
http://100.98.75.49:8080
```

Install Tailscale on your phone (iOS App Store / Google Play), sign in
with the same account as your VPS nodes. Your phone gets a 100.x.x.x IP
automatically. No firewall changes needed — `iif tailscale0 accept` already
covers it in nftables.

### Making access permanent (no terminal needed)

Run the port-forward as a systemd service on CP so it survives reboots:

```bash
sudo nano /etc/systemd/system/screener-dashboard.service
```

```ini
[Unit]
Description=Screener dashboard port-forward
After=network.target k3s.service
Wants=k3s.service

[Service]
ExecStart=/usr/local/bin/kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml \
  port-forward svc/screener-dashboard 8080:8080 -n screener --address 0.0.0.0
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now screener-dashboard
sudo systemctl status screener-dashboard
```

Then from any Tailscale device: `http://100.98.75.49:8080`

---

## External Access — Cloudflare Tunnel

When you have a domain, this gives you a permanent trusted HTTPS URL
accessible from anywhere — no Tailscale needed, no open ports.

### Prerequisites

- A domain added to Cloudflare (free plan works)
- `cloudflared` installed on CP (already installed from earlier session)

Verify: `cloudflared --version`

### Setup

```bash
# On CP — log in to your Cloudflare account
cloudflared tunnel login
# Opens a browser URL — visit it and authorize
```

```bash
# Create a named tunnel (one-time)
cloudflared tunnel create screener

# Note the tunnel ID shown — you'll need it below
# Also creates credentials file at ~/.cloudflared/<tunnel-id>.json
```

```bash
# Route your subdomain to the tunnel
cloudflared tunnel route dns screener dashboard.yourdomain.com
```

```bash
# Create tunnel config
mkdir -p ~/.cloudflared
nano ~/.cloudflared/config.yml
```

```yaml
tunnel: screener
credentials-file: /root/.cloudflared/<YOUR-TUNNEL-ID>.json

ingress:
  - hostname: dashboard.yourdomain.com
    service: http://localhost:8080
  - service: http_status:404
```

```bash
# Install as systemd service (permanent, survives reboots)
cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared
```

Your dashboard is now at `https://dashboard.yourdomain.com` — trusted
HTTPS, accessible from any browser anywhere in the world.

### Important note on the free trycloudflare.com tunnel

The quick tunnel `cloudflared tunnel --url http://localhost:8080` gives
a random URL that changes every restart. It works for testing but not
production. The named tunnel above is the correct permanent solution.

---

## Dashboard Password Protection

### Option 1 — Cloudflare Zero Trust (recommended, free)

After setting up Cloudflare Tunnel, in Cloudflare dashboard:
- Zero Trust → Access → Applications → Add application
- Select your dashboard URL
- Set policy: allow only your email address
- Users must authenticate with their Cloudflare/Google account

This is enterprise-grade access control at zero cost. No code changes needed.

### Option 2 — Flask HTTP Basic Auth (code change)

Add to `dashboard.py`:

```bash
# Add to requirements.txt
flask-httpauth==4.8.0
```

```python
# In dashboard.py
from flask_httpauth import HTTPBasicAuth
auth = HTTPBasicAuth()

@auth.verify_password
def verify(username, password):
    return (username == os.environ.get("DASHBOARD_USER", "phil") and
            password == os.environ.get("DASHBOARD_PASSWORD", ""))

@app.route("/")
@auth.login_required
def index():
    ...

@app.route("/health")
def health():  # health check must stay unprotected
    return jsonify({"status": "ok"})
```

Add `DASHBOARD_PASSWORD` to your Kubernetes secret in `k8s.local.yaml`,
rebuild the image, and redeploy.

---

## Dashboard Redesign (future)

The current dashboard is a single HTML string in `dashboard.py`. To make
it more visual and appealing you have two approaches:

### Simple — Edit the HTML/CSS directly

The entire UI lives in the `HTML = """..."""` string in `dashboard.py`.
You can swap in any CSS framework (Bootstrap, Tailwind, Bulma), add
Chart.js charts for multiplier history, or completely restyle it.
Rebuild the image after changes.

### Advanced — Split into API + frontend

```
dashboard.py       — Flask API only, serves /api/data as JSON
static/index.html  — your custom frontend, fetches from /api/data
```

Design `index.html` with any tool — plain HTML, Bootstrap Studio,
or Webflow export. The frontend calls your API every 60 seconds
for fresh data. This is the professional pattern for a live trading dashboard.

### Grafana (most powerful)

Deploy Grafana as a k3s pod, connect it to your PostgreSQL.
Your database schema is already well-structured for Grafana.
Drag-and-drop panel builder, built-in alerting, beautiful charts.
Good option once you have more historical data to visualize.

---

## Tuning Without Rebuilding

Edit the `config.yaml:` section inside `k8s.local.yaml`, then:

```bash
sudo kubectl apply -f k8s.local.yaml
sudo kubectl rollout restart deployment/memecoin-screener -n screener
```

Common tuning actions:

```yaml
# Disable Strategy A while keeping B
strategy_a:
  enabled: false

# Lower minimum score to get more alerts
strategy_b:
  scoring:
    min_score: 0.55   # was 0.62

# Widen age window for Strategy B
strategy_b:
  age:
    max_hours: 168   # 7 days instead of 5

# Reduce minimum volume if market is quiet
strategy_a:
  volume:
    min_24h: 30000   # was 50000
    min_1h: 3000     # was 5000
```

---

## Useful Commands

```bash
# Pod status
sudo kubectl get pods -n screener
sudo kubectl get pods -n screener -o wide

# Screener logs (live)
sudo kubectl logs -f deploy/memecoin-screener -c screener -n screener

# Dashboard logs
sudo kubectl logs -f deploy/memecoin-screener -c dashboard -n screener

# Postgres shell
sudo kubectl exec -it postgres-0 -n screener -- psql -U screener -d screener

# Useful SQL queries
# All alerts with live multiplier
SELECT symbol, strategy, mc_at_alert, current_mc,
       multiplier_vs_alert, trend
FROM alerts a
LEFT JOIN live_metrics lm ON lm.alert_id = a.id
ORDER BY multiplier_vs_alert DESC NULLS LAST;

# Strategy performance summary
SELECT strategy,
       COUNT(*) as alerts,
       ROUND(AVG(multiplier_vs_alert)::numeric, 2) as avg_mult,
       COUNT(*) FILTER (WHERE outcome = 'moon') as moon,
       COUNT(*) FILTER (WHERE outcome = 'up') as up
FROM alerts a
LEFT JOIN outcomes o ON o.alert_id = a.id
GROUP BY strategy;

# Restart after config change
sudo kubectl rollout restart deployment/memecoin-screener -n screener
sudo kubectl rollout status deployment/memecoin-screener -n screener

# Force delete stuck pod
sudo kubectl delete pod -n screener <pod-name> --force

# Check storage
sudo kubectl get pvc -n screener

# Teardown everything
sudo kubectl delete namespace screener
```

---

## Troubleshooting

### Pod won't schedule (Pending)
```bash
sudo kubectl describe pod -l app=memecoin-screener -n screener | grep -A5 Events
```
Usually `nodeSelector` hostname mismatch. Verify:
```bash
sudo kubectl get nodes
grep "hostname" k8s.local.yaml
```
Must match exactly — `worker-contabo`.

### Permission denied on Python files
The container runs as UID 1000 but files are owned by root.
Fix in Dockerfile:
```dockerfile
RUN mkdir -p /app/logs && chmod 777 /app/logs && chmod 644 /app/*.py
```
Rebuild and push.

### DB: password authentication failed
Passwords in `k8s.local.yaml` and `postgres.local.yaml` don't match.
Fix, then:
```bash
sudo kubectl delete secret screener-secrets postgres-secret -n screener
sudo kubectl apply -f postgres.local.yaml
sudo kubectl apply -f k8s.local.yaml
sudo kubectl rollout restart deployment/memecoin-screener -n screener
```

### DNS resolution failing inside pods
Pods can't reach `postgres.screener.svc.cluster.local`.
Check nftables rules on **both** nodes:
```bash
sudo nft list table inet filter | grep -E "10.42|10.43"
```
If missing, reload:
```bash
sudo nft -f /etc/nftables.conf
```

### Cross-node pod networking broken (ping fails between pods)
Flannel not using Tailscale interface. Verify k3s config on both nodes:
```bash
cat /etc/rancher/k3s/config.yaml
# Must contain: flannel-iface: tailscale0
```
If missing, add it and restart k3s/k3s-agent.

### nftables rules lost after reboot
```bash
sudo systemctl status nftables
sudo systemctl enable --now nftables
```

### kubectl logs returns 502 Bad Gateway
Worker node registered with public IP instead of Tailscale IP.
Check:
```bash
sudo kubectl get nodes -o wide
# INTERNAL-IP must be 100.x.x.x for both nodes
```
Fix on affected node:
```bash
# Add to /etc/rancher/k3s/config.yaml
node-ip: <tailscale-ip>
# Restart k3s or k3s-agent
```

### Worker still dialing public IP
Stale load balancer cache. Fix:
```bash
sudo systemctl stop k3s-agent
sudo nano /var/lib/rancher/k3s/agent/etc/k3s-agent-load-balancer.json
# Replace public IP with CP Tailscale IP (100.98.75.49)
sudo systemctl start k3s-agent
```

---

## Known Issues and Lessons Learned

**Code via ConfigMap mounts causes permission errors**
Never mount Python code via ConfigMaps when the container runs as a
non-root user. The kubelet creates ConfigMap mounts with root ownership
and the running user (UID 1000) can't read them regardless of `defaultMode`
or `fsGroup`. Always bake code into the Docker image. Only config files
(YAML, not Python) should be ConfigMap mounts.

**NodePort doesn't work reliably in k3s + Tailscale**
k3s uses kube-router instead of kube-proxy. NodePort services don't route
correctly when nodes register with Tailscale IPs. Use `kubectl port-forward`
via systemd service instead.

**Flannel defaults to public IP**
When k3s is installed without `--flannel-iface=tailscale0`, Flannel
uses the public IP for VXLAN traffic, which gets blocked by nftables.
Always set `flannel-iface: tailscale0` in `/etc/rancher/k3s/config.yaml`
before or immediately after installing k3s.

**Tailscale HTTPS requires a paid plan**
`tailscale cert` doesn't work on the free tier. Use Cloudflare Tunnel
with a domain for trusted HTTPS instead.

**nftables pod/service CIDRs must be on both nodes**
The firewall rules allowing `10.42.0.0/16` and `10.43.0.0/16` must
be present on the worker node too, not just the CP. Without them,
worker pods can't reach the Kubernetes API server (10.43.0.1) and
all system components crash-loop.

---

## Future Roadmap

### Near term
- [ ] Add Cloudflare Tunnel once domain is purchased
- [ ] Add Flask HTTP Basic Auth to dashboard
- [ ] Monitor screener performance for 2 weeks, tune filter thresholds
- [ ] Add ETH/BSC chain support (already in `CHAIN_TO_GECKO` map)

### Medium term
- [ ] Redesign dashboard with Chart.js — multiplier history charts,
      strategy comparison graphs, hit rate over time
- [ ] n8n integration — pipe screener alerts into n8n for AI analysis
      via local Ollama/Gemma model running on the cluster
- [ ] Upgrade worker VPS RAM (8GB → 16GB) to run `qwen3.5:9b` for
      better AI analysis quality

### Long term
- [ ] Smart wallet tracking — identify wallets that consistently buy
      early, use their activity as a filter layer
- [ ] Signal confidence scoring using historical outcome data
- [ ] Webhook output so other systems can consume alerts

---

## Infrastructure Summary

| Component | Details |
|---|---|
| CP node | cp-contabo, Contabo VPS, Tailscale 100.98.75.49 |
| Worker node | worker-contabo, Contabo VPS, Tailscale 100.74.235.111 |
| k3s version | v1.34.6+k3s1 |
| Firewall | nftables, default drop, Tailscale-only SSH |
| CNI | Flannel over tailscale0 (WireGuard) |
| Storage | k3s local-path provisioner |
| Container registry | Docker Hub (covenantwealth/memecoin-screener) |
| SSH port CP | 60607 |
| SSH port Worker | 60608 |
