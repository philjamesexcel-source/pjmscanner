# Memecoin Screener — Deployment Guide

## Strategy Overview

| | Strategy A (Fast) | Strategy B (Swing) |
|---|---|---|
| Token age | 5min – 24hr | 6hr – 120hr (5 days) |
| Risk | Higher | Lower |
| Security | Relaxed | Strict |
| Filters | 8 layers | 8 layers |
| Pullback watch | 12 hours | 48 hours |

**Both strategies run simultaneously in one pod.**

## Alert Types

1. **Initial Alert** — token passed all filters. Records MC at alert.
2. **Pullback Entry Alert** — token drops 15–55% (A) or 20–60% (B) from
   peak with volume — potential buy zone.
3. **Milestone Alert** — 2x, 5x, 10x vs initial MC, or 2x, 3x, 5x vs
   pullback entry price.
4. **72h Outcome** — final result 72 hours after initial alert.

## Architecture

```
worker-contabo pod
├── screener container   — scan loop + both strategies + tracker thread
└── dashboard container  — Flask UI port 8080

postgres StatefulSet     — persistence (local-path storage)
```

All Python code is **baked into the Docker image**.
Only `config.yaml` is mounted via ConfigMap — edit thresholds without rebuilding.

---

## Step 1 — Rotate your API keys

Before deploying, rotate these (they were previously exposed):
- Helius API key → helius.dev dashboard
- Telegram bot token → BotFather → /revoke

---

## Step 2 — Fill in secrets

Edit both files. Replace all `REPLACE_WITH_*` values.

`postgres.yaml` — one value: `POSTGRES_PASSWORD`

`k8s.local.yaml` — four values:
```
TELEGRAM_BOT_TOKEN
TELEGRAM_CHANNEL_ID
HELIUS_RPC_URL
POSTGRES_PASSWORD   (must match postgres.yaml)
```

---

## Step 3 — Build and push Docker image

```bash
cd ~/memescreener

docker build -t covenantwealth/memecoin-screener:v1 .
docker push covenantwealth/memecoin-screener:v1
```

---

## Step 4 — Deploy PostgreSQL

```bash
sudo kubectl apply -f postgres.local.yaml
sudo kubectl rollout status statefulset/postgres -n screener
```

---

## Step 5 — Deploy the screener

```bash
sudo kubectl apply -f k8s.local.yaml
sudo kubectl get pods -n screener -w
```

Expected:
```
postgres-0                           1/1   Running   0   3m
memecoin-screener-xxxxxxxxx-xxxxx    2/2   Running   0   60s
```

`2/2` = screener + dashboard both running.

---

## Step 6 — Access the dashboard

On CP:
```bash
sudo kubectl port-forward svc/screener-dashboard 8080:8080 -n screener --address 127.0.0.1
```

On your desktop:
```bash
ssh -N -L 8080:127.0.0.1:8080 phil@100.98.75.49 -p 60607 -i ~/.ssh/contabo
```

Open: `http://localhost:8080`

---

## Step 7 — Verify it's working

```bash
# See scan activity
sudo kubectl logs -f deploy/memecoin-screener -c screener -n screener

# Check for errors
sudo kubectl logs -f deploy/memecoin-screener -c dashboard -n screener
```

A startup Telegram message should arrive within 30 seconds.

---

## Tuning thresholds (no rebuild needed)

Edit the ConfigMap in `k8s.local.yaml` — find the `config.yaml:` section
and change any threshold. Then apply and restart:

```bash
sudo kubectl apply -f k8s.local.yaml
sudo kubectl rollout restart deployment/memecoin-screener -n screener
```

To disable Strategy A while keeping B running:
```yaml
strategy_a:
  enabled: false
```

---

## Useful commands

```bash
# Pod status
sudo kubectl get pods -n screener

# Screener logs
sudo kubectl logs -f deploy/memecoin-screener -c screener -n screener

# Dashboard logs
sudo kubectl logs -f deploy/memecoin-screener -c dashboard -n screener

# Postgres shell
sudo kubectl exec -it postgres-0 -n screener -- psql -U screener -d screener

# Restart after config change
sudo kubectl rollout restart deployment/memecoin-screener -n screener

# Teardown
sudo kubectl delete namespace screener
```

---

## Git workflow

```bash
# Templates (no secrets) — safe to commit
git add k8s.yaml postgres.yaml

# Local files with real secrets — NEVER commit
# k8s.local.yaml
# postgres.local.yaml
```

The pre-commit hook in `check_secrets.sh` will block you if you
accidentally try to commit a file with real credentials.
