#!/usr/bin/env python3
"""
dashboard.py — Flask web UI.
Two strategy tabs (A and B), live metrics, pullback alerts, milestones.
Access via SSH tunnel: ssh -N -L 8080:127.0.0.1:8080 ...
"""

import os
import logging
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify
import db

app = Flask(__name__)

# ─────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Memecoin Screener</title>
<meta http-equiv="refresh" content="60">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 13px; }
  .header { background: #161b22; padding: 16px 24px; border-bottom: 1px solid #30363d; }
  .header h1 { font-size: 18px; color: #58a6ff; }
  .header .subtitle { color: #8b949e; font-size: 12px; margin-top: 4px; }
  .tabs { display: flex; gap: 0; background: #161b22; border-bottom: 1px solid #30363d; }
  .tab { padding: 12px 24px; cursor: pointer; border-bottom: 2px solid transparent; color: #8b949e; }
  .tab.active { border-bottom-color: #58a6ff; color: #e6edf3; }
  .content { display: none; padding: 20px 24px; }
  .content.active { display: block; }
  .summary-cards { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px 18px; min-width: 140px; }
  .card .label { color: #8b949e; font-size: 11px; text-transform: uppercase; margin-bottom: 6px; }
  .card .value { font-size: 22px; font-weight: bold; color: #e6edf3; }
  .card .value.green { color: #3fb950; }
  .card .value.red { color: #f85149; }
  .card .value.yellow { color: #d29922; }
  table { width: 100%; border-collapse: collapse; }
  th { background: #161b22; color: #8b949e; text-align: left; padding: 10px 12px; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #30363d; position: sticky; top: 0; }
  td { padding: 10px 12px; border-bottom: 1px solid #21262d; vertical-align: middle; }
  tr:hover td { background: #161b22; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge-a { background: #1f4a6e; color: #58a6ff; }
  .badge-b { background: #2d3b1f; color: #3fb950; }
  .badge-moon { background: #2d1b69; color: #a78bfa; }
  .badge-up { background: #1a3a1a; color: #3fb950; }
  .badge-flat { background: #2d2a1f; color: #d29922; }
  .badge-down { background: #3a1a1a; color: #f85149; }
  .badge-dead { background: #2a2a2a; color: #8b949e; }
  .mult { font-weight: bold; }
  .mult-moon { color: #a78bfa; }
  .mult-up { color: #3fb950; }
  .mult-flat { color: #d29922; }
  .mult-down { color: #f85149; }
  .trend-up { color: #3fb950; }
  .trend-down { color: #f85149; }
  .trend-flat { color: #8b949e; }
  .pb-alert { background: #1f3a5c; border-left: 3px solid #58a6ff; padding: 4px 8px; border-radius: 4px; font-size: 11px; margin-top: 4px; }
  .milestone { background: #2d1b69; border-left: 3px solid #a78bfa; padding: 4px 8px; border-radius: 4px; font-size: 11px; margin-top: 4px; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .stale { color: #8b949e; font-style: italic; font-size: 11px; }
  .section-title { color: #8b949e; font-size: 11px; text-transform: uppercase; margin: 20px 0 10px; }
  .empty { text-align: center; color: #8b949e; padding: 40px; }
</style>
</head>
<body>

<div class="header">
  <h1>🦞 Memecoin Screener</h1>
  <div class="subtitle">Last refresh: {{ now }} UTC &nbsp;|&nbsp; Auto-refreshes every 60s</div>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('a', this)">⚡ Strategy A — Fast</div>
  <div class="tab" onclick="showTab('b', this)">🎯 Strategy B — Swing</div>
  <div class="tab" onclick="showTab('all', this)">📊 All Coins</div>
</div>

{% for tab_id, tab_label, rows, summary in tabs %}
<div id="tab-{{ tab_id }}" class="content {% if loop.first %}active{% endif %}">

  <!-- Summary Cards -->
  <div class="summary-cards">
    <div class="card">
      <div class="label">Total Alerts</div>
      <div class="value">{{ summary.total_alerts or 0 }}</div>
    </div>
    <div class="card">
      <div class="label">Outcomes In</div>
      <div class="value">{{ summary.total_checked or 0 }}</div>
    </div>
    <div class="card">
      <div class="label">Avg Multiplier</div>
      <div class="value {% if (summary.avg_mult or 0) >= 2 %}green{% elif (summary.avg_mult or 0) >= 1 %}yellow{% else %}red{% endif %}">
        {{ "%.2f"|format(summary.avg_mult or 0) }}x
      </div>
    </div>
    <div class="card">
      <div class="label">🚀 Moon (5x+)</div>
      <div class="value green">{{ summary.moon_count or 0 }}</div>
    </div>
    <div class="card">
      <div class="label">📈 Up (2-5x)</div>
      <div class="value green">{{ summary.up_count or 0 }}</div>
    </div>
    <div class="card">
      <div class="label">💀 Dead/Down</div>
      <div class="value red">{{ (summary.dead_count or 0) + (summary.down_count or 0) }}</div>
    </div>
    <div class="card">
      <div class="label">Pullback Alerts</div>
      <div class="value yellow">{{ summary.pullback_alerts_sent or 0 }}</div>
    </div>
  </div>

  {% if rows %}
  <table>
    <thead>
      <tr>
        <th>Token</th>
        <th>Strategy</th>
        <th>MC at Alert</th>
        <th>Peak MC</th>
        <th>Current MC</th>
        <th>vs Alert</th>
        <th>vs Entry</th>
        <th>Vol 5m</th>
        <th>1h Chg</th>
        <th>Trend</th>
        <th>Pullback Entry</th>
        <th>72h Outcome</th>
        <th>Alerted</th>
        <th>Links</th>
      </tr>
    </thead>
    <tbody>
    {% for r in rows %}
    <tr>
      <td>
        <b>{{ r.symbol }}</b><br>
        <span style="color:#8b949e;font-size:11px">{{ r.name[:20] }}</span>
      </td>
      <td>
        <span class="badge {% if r.strategy == 'A' %}badge-a{% else %}badge-b{% endif %}">
          {{ r.strategy }}
        </span>
      </td>
      <td>${{ "{:,.0f}".format(r.mc_at_alert or 0) }}</td>
      <td>
        {% if r.peak_mc %}
          ${{ "{:,.0f}".format(r.peak_mc) }}
          {% if r.mc_at_alert and r.mc_at_alert > 0 %}
            <br><span style="color:#8b949e;font-size:11px">
              {{ "%.1f"|format(r.peak_mc / r.mc_at_alert) }}x peak
            </span>
          {% endif %}
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.current_mc %}
          ${{ "{:,.0f}".format(r.current_mc) }}
        {% else %}<span class="stale">stale</span>{% endif %}
      </td>
      <td>
        {% if r.live_mult %}
          {% set m = r.live_mult %}
          <span class="mult {% if m >= 5 %}mult-moon{% elif m >= 2 %}mult-up{% elif m >= 0.8 %}mult-flat{% else %}mult-down{% endif %}">
            {{ "%.2f"|format(m) }}x
          </span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.live_mult_entry %}
          <span class="mult {% if r.live_mult_entry >= 2 %}mult-up{% elif r.live_mult_entry >= 0.8 %}mult-flat{% else %}mult-down{% endif %}">
            {{ "%.2f"|format(r.live_mult_entry) }}x
          </span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.live_vol_5m %}${{ "{:,.0f}".format(r.live_vol_5m) }}{% else %}—{% endif %}
      </td>
      <td>
        {% if r.price_change_1h is not none %}
          <span class="{% if r.price_change_1h >= 0 %}trend-up{% else %}trend-down{% endif %}">
            {{ "%+.1f"|format(r.price_change_1h) }}%
          </span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.trend %}
          <span class="trend-{{ r.trend }}">
            {% if r.trend == 'up' %}↑{% elif r.trend == 'down' %}↓{% else %}→{% endif %}
          </span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.pullback_sent_at %}
          <div class="pb-alert">
            📉 {{ "%.1f"|format(r.pullback_pct or 0) }}% pullback<br>
            ${{ "{:,.0f}".format(r.mc_at_pullback or 0) }} MC<br>
            <span style="color:#8b949e">{{ r.pullback_sent_at.strftime('%m/%d %H:%M') }}</span>
          </div>
        {% elif r.pullback_watching %}
          <span style="color:#8b949e;font-size:11px">👀 watching</span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.outcome %}
          <span class="badge badge-{{ r.outcome }}">{{ r.outcome }}</span>
          {% if r.mult_72h %}
            <br><span style="font-size:11px">{{ "%.2f"|format(r.mult_72h) }}x</span>
          {% endif %}
        {% elif r.check_due_at %}
          {% set now_ts = now_ts %}
          <span style="color:#8b949e;font-size:11px">
            due {{ r.check_due_at.strftime('%m/%d %H:%M') }}
          </span>
        {% else %}—{% endif %}
      </td>
      <td>
        <span style="color:#8b949e;font-size:11px">
          {{ r.alerted_at.strftime('%m/%d %H:%M') if r.alerted_at else '—' }}
        </span>
      </td>
      <td>
        <a href="https://dexscreener.com/solana/{{ r.pair_addr }}" target="_blank">DEX</a>
        &nbsp;
        <a href="https://rugcheck.xyz/tokens/{{ r.mint }}" target="_blank">RC</a>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No alerts yet for this strategy.</div>
  {% endif %}

</div>
{% endfor %}

<script>
function showTab(id, el) {
  document.querySelectorAll('.content').forEach(c => c.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  el.classList.add('active');
}
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def index():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    tabs = []
    for tab_id, tab_label, strategy_filter in [
        ("a", "⚡ Strategy A — Fast", "A"),
        ("b", "🎯 Strategy B — Swing", "B"),
        ("all", "📊 All Coins", None),
    ]:
        rows    = db.get_dashboard_data(strategy_filter)
        summary = db.get_strategy_summary(strategy_filter) if strategy_filter else _all_summary()
        tabs.append((tab_id, tab_label, rows, summary))

    return render_template_string(
        HTML, tabs=tabs, now=now,
        now_ts=datetime.now(timezone.utc)
    )


def _all_summary() -> dict:
    a = db.get_strategy_summary("A")
    b = db.get_strategy_summary("B")

    def _add(key, default=0):
        return (a.get(key) or default) + (b.get(key) or default)

    total_checked = _add("total_checked")
    moon  = _add("moon_count")
    up    = _add("up_count")
    flat  = _add("flat_count")
    down  = _add("down_count")
    dead  = _add("dead_count")

    avg_a = float(a.get("avg_mult") or 0)
    avg_b = float(b.get("avg_mult") or 0)
    checked_a = int(a.get("total_checked") or 0)
    checked_b = int(b.get("total_checked") or 0)
    total_c = checked_a + checked_b
    avg = ((avg_a * checked_a) + (avg_b * checked_b)) / total_c if total_c > 0 else 0

    return {
        "total_alerts":         _add("total_alerts"),
        "total_checked":        total_checked,
        "avg_mult":             round(avg, 3),
        "moon_count":           moon,
        "up_count":             up,
        "flat_count":           flat,
        "down_count":           down,
        "dead_count":           dead,
        "pullback_alerts_sent": _add("pullback_alerts_sent"),
    }


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
