# Memecoin Screener

Solana memecoin signal system with two simultaneous strategies, pullback entry detection, milestone tracking, and a Flask dashboard.

## Architecture

```
worker-contabo (k3s pod)
├── screener container   — scan loop, both strategies, background tracker
└── dashboard container  — Flask UI, port 8080

postgres StatefulSet     — persistence via k3s local-path storage
```

## Two Strategies

| | Strategy A — Fast | Strategy B — Swing |
|---|---|---|
| Token age | 5min – 24hr | 6hr – 120hr |
| Risk | Higher | Lower |
| Security filters | Relaxed | Strict |
| Pullback watch window | 12 hours | 48 hours |

Both strategies run simultaneously. Either can be disabled in `config.yaml` without rebuilding.

## Three Alert Types

1. **Initial Alert** — token passed all 8 filter layers. Records MC at alert time.
2. **Pullback Entry Alert** — token drops 15–55% (A) or 20–60% (B) from peak with volume confirmation — potential buy zone.
3. **Milestone Alert** — 2x / 5x / 10x vs initial MC, or 2x / 3x / 5x vs pullback entry price.
4. **72h Outcome** — final result 72 hours after initial alert.

## File Structure

```
screener.py     — main scan loop, runs both strategies
filters.py      — 8 filter layers + composite scoring
tracker.py      — background thread: pullback, milestones, 72h outcomes
db.py           — PostgreSQL layer, full schema
alerts.py       — Telegram message builders for all alert types
dashboard.py    — Flask UI with Strategy A / Strategy B / All tabs
config.yaml     — all thresholds (mounted via ConfigMap, no rebuild needed)
Dockerfile      — all code baked in, no permission issues
requirements.txt
k8s.yaml        — template with REPLACE_WITH_* placeholders (safe to commit)
postgres.yaml   — template with REPLACE_WITH_* placeholders (safe to commit)
DEPLOY.md       — full deployment guide
check_secrets.sh — pre-commit hook, blocks accidental secret commits
```

## Security Model

- All traffic via Tailscale (WireGuard E2E encrypted)
- No public ports open
- Secrets stored as Kubernetes Secrets, never in committed YAML
- `k8s.local.yaml` and `postgres.local.yaml` hold real credentials — VPS only, never committed
- `check_secrets.sh` pre-commit hook blocks any accidental secret exposure

## Access

Dashboard via SSH tunnel:
```bash
# On desktop
ssh -N -L 8080:127.0.0.1:8080 phil@<CP_TS_IP> -p 60607 -i ~/.ssh/contabo
# Open http://localhost:8080
```

## Deployment

See `DEPLOY.md` for full step-by-step instructions.
