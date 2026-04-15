# PJM Scanner

A production-grade Solana memecoin intelligence platform running on Kubernetes.

Three simultaneous strategies detect high-probability opportunities, track performance from detection through entry, and deliver structured alerts to Telegram with full security analysis.

---

## What it does

The scanner runs continuously on a private k3s cluster, polling GeckoTerminal and DexScreener every 2 minutes. Each token is evaluated against three independent strategies in parallel. Tokens that pass all filter layers are scored 0–100 using a weighted composite model. Only tokens above the score threshold trigger a Telegram alert.

After detection, a background thread monitors every alerted token every 60 seconds — watching for pullback entry zones, firing milestone alerts at 2x/5x/10x/20x, and recording 72-hour outcomes automatically.

A Flask dashboard exposes live performance across all strategies, accessible via SSH tunnel or Cloudflare Tunnel with a domain.

---

## Strategies

| | Strategy A — Safe | Strategy B — Momentum | Strategy C — Second Wave |
|---|---|---|---|
| Token age | 6h – 72h | 1h – 24h | 6h – 5 days |
| MC range | $150K – $3M | $30K – $500K | $100K – $5M |
| Min liquidity | $50,000 | $15,000 | $40,000 |
| LP lock | Required (80%+) | Optional | Required |
| Security | Strict | Moderate | Strict |
| Min holders | 300 | 150 | 400 |
| Min score | 65/100 | 60/100 | 62/100 |
| Pullback watch | 48 hours | 8 hours | 48 hours |

Each strategy can be independently enabled or disabled in config without rebuilding the image.

---

## Alert Types

**1. Detection Alert** — Token passed all filters. Includes full checklist, deployer link, LP lock status, buy/sell signal, composite score breakdown, and 7 trading buttons.

**2. Pullback Entry Alert** — Token has dropped 15–45% from peak with volume confirmation. Shows MC at detection, peak MC reached, and current entry price.

**3. Milestone Alert** — Fires at 2x / 5x / 10x / 20x vs detection MC, and 2x / 3x / 5x / 10x vs pullback entry price.

**4. 72h Outcome** — Final result 72 hours after detection. Classified as moon (5x+) / up (2–5x) / flat / down / dead.

**5. Smart Wallet Signal** — Fires when 2+ tracked wallets buy the same token (Helius RPC).

**6. Interim Snapshot** — Daily summary at 23:00 EAT showing all tracked tokens and current multiples.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  GeckoTerminal          DexScreener          RugCheck        │
│  new_pools + trending   market data          security score  │
└──────────────┬──────────────────┬────────────────┬──────────┘
               │                  │                │
               ▼                  ▼                ▼
┌─────────────────────────────────────────────────────────────┐
│  Discovery Pipeline  (data/dexscreener.py)                  │
│  Gecko → mint list → DexScreener enrichment → candidates    │
└─────────────────────────────┬───────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        Strategy A      Strategy B      Strategy C
        Safe            Momentum        Second Wave
        8 filters       8 filters       8 filters
              └───────────────┼───────────────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │  Composite Scorer (0–100)     │
              │  Liquidity  0.20              │
              │  Volume     0.20              │
              │  Momentum   0.25              │
              │  Holders    0.15              │
              │  Wallets    0.10              │
              │  Risk       0.10              │
              └───────────────┬───────────────┘
                              │ score >= threshold
                              ▼
              ┌───────────────────────────────┐
              │  Telegram Detection Alert     │
              │  + DB insert (tokens table)   │
              └───────────────┬───────────────┘
                              │
              ┌───────────────▼───────────────┐
              │  Background Tracker (60s)     │
              │  • Update peak/lowest price   │
              │  • Pullback entry detection   │
              │  • Milestone alerts           │
              │  • 72h outcome processing     │
              │  • Interim snapshots          │
              └───────────────┬───────────────┘
                              │
              ┌───────────────▼───────────────┐
              │  PostgreSQL                   │
              │  tokens | entry_signals       │
              │  performance_tracking         │
              │  milestones | outcomes        │
              │  wallets | wallet_trades      │
              └───────────────────────────────┘
```

### Infrastructure

```
cp-contabo (k3s control plane)
  Tailscale: 100.98.75.49 | SSH: 60607

worker-contabo (k3s worker — all pods run here)
  Tailscale: 100.74.235.111 | SSH: 60608

Pod: pjm-scanner
  ├── scanner container    main loop + tracker thread + wallet tracker
  └── dashboard container  Flask UI on port 8080

Pod: postgres StatefulSet
  local-path storage at /var/lib/rancher/k3s/storage/

All inter-node traffic routed over Flannel via tailscale0 (WireGuard).
No public ports open. Dashboard accessed via SSH tunnel or Cloudflare Tunnel.
```

---

## Project Structure

```
pjmscanner/
├── main.py                    Orchestration loop
├── tracker_loop.py            Background: pullbacks, milestones, 72h outcomes
├── Dockerfile
├── requirements.txt
│
├── core/
│   ├── config.py              Config loader with version history and auto-revert
│   ├── circuit_breaker.py     Per-API circuit breaker (CLOSED / OPEN / HALF_OPEN)
│   ├── rate_limiter.py        Token bucket rate limiter per API
│   └── database.py            Full PostgreSQL schema and all queries
│
├── data/
│   ├── dexscreener.py         GeckoTerminal discovery + DexScreener enrichment
│   └── rugcheck.py            Security report fetcher and parser
│
├── strategies/
│   ├── base.py                Shared filter pipeline
│   ├── strategy_a.py          Safe — Raydium/bonded tokens
│   ├── strategy_b.py          Momentum — 1h–24h tokens
│   └── strategy_c.py          Second Wave — reaccumulation pattern
│
├── scoring/
│   └── scorer.py              Composite score 0–100 with 6 weighted components
│
├── alerts/
│   └── telegram.py            All 6 alert types with rich HTML format
│
├── wallet_tracker/
│   └── tracker.py             Helius RPC wallet monitoring
│
├── dashboard/
│   └── app.py                 Flask UI — 6 tabs (A / B / C / All / Top / Wallets)
│
├── config/
│   ├── global_config.yaml     Baseline filters, scoring weights, schedule
│   ├── strategy_a.yaml        Strategy A thresholds
│   ├── strategy_b.yaml        Strategy B thresholds
│   ├── strategy_c.yaml        Strategy C thresholds
│   └── wallet_tracking.yaml   Wallet qualification and scoring
│
└── k8s/
    ├── postgres.yaml          StatefulSet with local-path storage (template)
    └── deployment.yaml        2-container deployment + ConfigMap (template)
```

---

## Reliability Features

**Circuit breakers** — Each external API has an independent circuit breaker. After 5 consecutive failures the circuit opens and calls are blocked for 30 seconds, preventing cascade failures from one bad API affecting others. Auto-recovers on success.

**Rate limiters** — Token bucket rate limiter per API service. DexScreener: 2 req/s. RugCheck: 1 req/s. Helius: 5 req/s. Telegram: 0.5 req/s. Prevents 429 errors under sustained load.

**Config versioning + auto-revert** — Every config load saves a versioned JSON snapshot to disk. If the scanner crashes 3+ times within 5 minutes, it automatically reverts to the previous known-stable config and logs the rollback. Config version history is kept for the last 10 changes.

**Health endpoint** — Port 9090 serves a JSON health response including all circuit breaker states. k3s liveness probe pings this every 30 seconds and auto-restarts the pod if it fails.

**Retry logic** — All API calls retry up to 3 times with exponential backoff (1s → 2s → 4s) before triggering the circuit breaker.

---

## Secret Management

The repo contains only template files with `REPLACE_WITH_*` placeholders. Real credentials live in `*.local.yaml` files which are gitignored and exist only on the VPS.

```
Committed to git (safe):          VPS only (never committed):
k8s/deployment.yaml               k8s/deployment.local.yaml
k8s/postgres.yaml                 k8s/postgres.local.yaml
```

`check_secrets.sh` runs as a git pre-commit hook and blocks any commit containing Telegram bot token patterns, Helius API key UUIDs, filled-in YAML secrets, or template files where `REPLACE_WITH_` placeholders have been removed.

Install the hook after cloning:
```bash
mkdir -p .git/hooks
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash
bash check_secrets.sh
EOF
chmod +x .git/hooks/pre-commit
chmod +x check_secrets.sh
```

---

## Deployment

### Prerequisites

- k3s cluster on two nodes with `flannel-iface: tailscale0` configured on both
- Both nodes have nftables rules allowing pod and service CIDRs (`10.42.0.0/16` and `10.43.0.0/16`)
- Docker Hub account
- Telegram bot token and channel ID
- Helius API key (free tier at helius.dev)

### Step 1 — Clone and set up

```bash
git clone git@github.com:philjamesexcel-source/pjmscanner.git
cd pjmscanner

# Install pre-commit hook
mkdir -p .git/hooks
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash
bash check_secrets.sh
EOF
chmod +x .git/hooks/pre-commit

# Create local secret files
cp k8s/deployment.yaml k8s/deployment.local.yaml
cp k8s/postgres.yaml   k8s/postgres.local.yaml

nano k8s/postgres.local.yaml     # set POSTGRES_PASSWORD
nano k8s/deployment.local.yaml   # set TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID,
                                 # HELIUS_RPC_URL, POSTGRES_PASSWORD
```

### Step 2 — Build and push image

```bash
docker build -t covenantwealth/pjm-scanner:v1 .
docker push covenantwealth/pjm-scanner:v1
```

### Step 3 — Deploy

```bash
# On CP — deploy Postgres first
sudo kubectl apply -f k8s/postgres.local.yaml
sudo kubectl rollout status statefulset/postgres -n screener

# Deploy scanner
sudo kubectl apply -f k8s/deployment.local.yaml
sudo kubectl get pods -n screener -w
```

Expected final state:
```
NAME                          READY   STATUS    RESTARTS   AGE
pjm-scanner-xxxxxxxxx-xxxxx   2/2     Running   0          60s
postgres-0                    1/1     Running   0          3m
```

`2/2` = scanner + dashboard both running.

### Step 4 — Access the dashboard

On CP:
```bash
sudo kubectl port-forward svc/scanner-dashboard 8080:8080 -n screener --address 127.0.0.1
```

On desktop:
```bash
ssh -N -L 8080:127.0.0.1:8080 phil@100.98.75.49 -p 60607 -i ~/.ssh/contabo
```

Open: `http://localhost:8080`

---

## Tuning Thresholds

All thresholds live in the `scanner-config` ConfigMap inside `k8s/deployment.local.yaml`. Edit the config YAML inline and apply — no rebuild needed.

```bash
# After editing deployment.local.yaml
sudo kubectl apply -f k8s/deployment.local.yaml
sudo kubectl rollout restart deployment/pjm-scanner -n screener
```

To disable a strategy while keeping others running:
```yaml
strategy_b.yaml: |
  enabled: false
```

---

## Useful Commands

```bash
# Pod status
sudo kubectl get pods -n screener

# Live scanner logs
sudo kubectl logs -f deploy/pjm-scanner -c scanner -n screener

# Dashboard logs
sudo kubectl logs -f deploy/pjm-scanner -c dashboard -n screener

# Postgres shell
sudo kubectl exec -it postgres-0 -n screener -- psql -U screener -d screener

# Top performers query
SELECT symbol, strategy, score,
       mc_at_detection, pt.current_mc,
       pt.multiple_vs_detection, pt.outcome
FROM tokens t
LEFT JOIN performance_tracking pt ON pt.token_id = t.id
ORDER BY pt.multiple_vs_detection DESC NULLS LAST
LIMIT 20;

# Restart after config change
sudo kubectl rollout restart deployment/pjm-scanner -n screener

# Full teardown
sudo kubectl delete namespace screener
```

---

## Updating Code

```bash
# 1. Edit files on desktop, commit, push
git add .
git commit -m "fix: description"
git push origin main

# 2. On CP — pull latest
cd ~/pjmscanner && git pull origin main

# 3. On desktop — rebuild image with new version tag
docker build -t covenantwealth/pjm-scanner:v2 .
docker push covenantwealth/pjm-scanner:v2

# 4. Update image tag in deployment.local.yaml, then on CP:
sudo kubectl apply -f k8s/deployment.local.yaml
sudo kubectl rollout restart deployment/pjm-scanner -n screener
```

---

## Roadmap

- [ ] Cloudflare Tunnel + domain for permanent HTTPS dashboard access
- [ ] Flask HTTP Basic Auth on dashboard
- [ ] Chart.js performance charts — multiplier history, strategy comparison over time
- [ ] n8n integration — pipe alerts to AI analysis via local Ollama model on cluster
- [ ] Full Helius wallet transaction parsing for smart money tracking
- [ ] ETH and BSC chain support (placeholders already in codebase)
- [ ] Worker VPS RAM upgrade 8GB → 16GB for local LLM inference
- [ ] Grafana dashboard connected to PostgreSQL for advanced visualizations

---

## Data Sources

| Source | Used for | Auth required |
|---|---|---|
| GeckoTerminal | Token discovery (new + trending pools) | No |
| DexScreener | Market data, volume, txns, price change | No |
| RugCheck | Security scoring, holder analysis, LP lock | No |
| Helius RPC | On-chain wallet transaction history | API key |

---

## Tech Stack

Python 3.11 · Flask · PostgreSQL 15 · k3s · Docker · Tailscale · Telegram Bot API
