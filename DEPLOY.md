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
   peak with volume confirmation — potential buy zone.
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

## Git + Secret Protection

### Repository

```
GitHub:  git@github.com:philjamesexcel-source/pjmscanner.git
Local:   ~/pjmscanner
```

### The two-file pattern

Every secret-containing YAML has two versions:

| File | Contains | Committed? |
|---|---|---|
| `k8s.yaml` | `REPLACE_WITH_*` placeholders | ✅ Yes — safe |
| `k8s.local.yaml` | Real credentials | ❌ No — VPS only |
| `postgres.yaml` | `REPLACE_WITH_*` placeholders | ✅ Yes — safe |
| `postgres.local.yaml` | Real credentials | ❌ No — VPS only |

`.gitignore` contains `*.local.yaml` so git never sees the local files.

### How check_secrets.sh protects you

`check_secrets.sh` runs automatically before every `git commit` via a
Git pre-commit hook at `.git/hooks/pre-commit`. This means it fires
**before git writes the commit** — if it finds a secret, the commit
is blocked and nothing is recorded in git history.

What it checks:

```
Telegram bot token format    :[0-9]{10}:AA
Helius API key format        api-key=[uuid]
Filled-in bot token in YAML  TELEGRAM_BOT_TOKEN:...:AA
Filled-in channel ID         TELEGRAM_CHANNEL_ID:...-100[0-9]{10}
Template placeholders gone   k8s.yaml / postgres.yaml must still contain REPLACE_WITH_
```

The last check is the most important — if you accidentally fill in
real values directly into `k8s.yaml` instead of `k8s.local.yaml`,
the script detects that `REPLACE_WITH_` is no longer present and
blocks the commit.

**The hook is installed automatically** when you clone the repo
if you run the setup command below. It lives at `.git/hooks/pre-commit`
and is not committed to git itself (git hooks are local only).

### Setting up on a new machine after cloning

```bash
git clone git@github.com:philjamesexcel-source/pjmscanner.git
cd pjmscanner

# Install the pre-commit hook
mkdir -p .git/hooks
cat > .git/hooks/pre-commit << 'HOOK'
#!/bin/bash
bash check_secrets.sh
HOOK
chmod +x .git/hooks/pre-commit
chmod +x check_secrets.sh

# Create your local secret files
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

# Push to GitHub
git push origin main
```

### Pulling updates to the VPS

```bash
# On CP
cd ~/pjmscanner
git pull origin main

# Rebuild image if Python files changed
docker build -t covenantwealth/memecoin-screener:v2 .
docker push covenantwealth/memecoin-screener:v2

# Update image tag in k8s.local.yaml then redeploy
sudo kubectl apply -f k8s.local.yaml
sudo kubectl rollout restart deployment/memecoin-screener -n screener
```

### If only config.yaml changed (no rebuild needed)

```bash
# On CP
cd ~/pjmscanner
git pull origin main
sudo kubectl apply -f k8s.local.yaml   # updates the ConfigMap
sudo kubectl rollout restart deployment/memecoin-screener -n screener
```

---

## First Deployment Steps

### Step 1 — Rotate your API keys

Before deploying, rotate these (previously exposed in old repo):
- Helius API key → helius.dev dashboard → create new key
- Telegram bot token → BotFather → /revoke → create new

### Step 2 — Fill in secrets on VPS

```bash
# On CP — after cloning
cd ~/pjmscanner
cp k8s.yaml k8s.local.yaml
cp postgres.yaml postgres.local.yaml
nano k8s.local.yaml       # fill in REPLACE_WITH_* values
nano postgres.local.yaml  # fill in REPLACE_WITH_STRONG_PASSWORD
```

### Step 3 — Build and push Docker image

```bash
# On desktop
cd ~/pjmscanner
docker build -t covenantwealth/memecoin-screener:v1 .
docker push covenantwealth/memecoin-screener:v1
```

### Step 4 — Deploy PostgreSQL

```bash
# On CP
sudo kubectl apply -f postgres.local.yaml
sudo kubectl rollout status statefulset/postgres -n screener
```

### Step 5 — Deploy the screener

```bash
sudo kubectl apply -f k8s.local.yaml
sudo kubectl get pods -n screener -w
```

Expected:
```
postgres-0                           1/1   Running   0   3m
memecoin-screener-xxxxxxxxx-xxxxx    2/2   Running   0   60s
```

`2/2` = screener + dashboard both running. ✅

### Step 6 — Access the dashboard

On CP (keep running):
```bash
sudo kubectl port-forward svc/screener-dashboard 8080:8080 -n screener --address 127.0.0.1
```

On desktop (keep running):
```bash
ssh -N -L 8080:127.0.0.1:8080 phil@<CP_TS_IP> -p 60607 -i ~/.ssh/contabo
```

Open: `http://localhost:8080`

### Step 7 — Verify

```bash
sudo kubectl logs -f deploy/memecoin-screener -c screener -n screener
```

A startup Telegram message should arrive within 30 seconds.

---

## Tuning thresholds (no rebuild needed)

Edit the `config.yaml:` section inside `k8s.local.yaml`, then:

```bash
sudo kubectl apply -f k8s.local.yaml
sudo kubectl rollout restart deployment/memecoin-screener -n screener
```

To disable Strategy A:
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
