"""
dashboard/app.py — Flask web dashboard.
Tabs: Strategy A | Strategy B | Strategy C | All | Top Performers | Wallets
"""

import os
import logging
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify
from core import database as db

app = Flask(__name__)


HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PJM Scanner</title>
<meta http-equiv="refresh" content="60">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 13px; }
.header { background: #161b22; padding: 16px 24px; border-bottom: 1px solid #30363d; display:flex; align-items:center; justify-content:space-between; }
.header h1 { font-size: 18px; color: #58a6ff; }
.header .meta { color: #8b949e; font-size: 11px; }
.tabs { display: flex; background: #161b22; border-bottom: 1px solid #30363d; overflow-x:auto; }
.tab { padding: 12px 20px; cursor: pointer; border-bottom: 2px solid transparent; color: #8b949e; white-space:nowrap; }
.tab.active { border-bottom-color: #58a6ff; color: #e6edf3; }
.content { display: none; padding: 16px 20px; }
.content.active { display: block; }
.cards { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 16px; min-width: 120px; }
.card .label { color: #8b949e; font-size: 10px; text-transform: uppercase; margin-bottom: 4px; }
.card .value { font-size: 20px; font-weight: bold; }
.green { color: #3fb950; }
.yellow { color: #d29922; }
.red { color: #f85149; }
.blue { color: #58a6ff; }
.purple { color: #a78bfa; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { background: #161b22; color: #8b949e; text-align: left; padding: 8px 10px; font-size: 10px; text-transform: uppercase; border-bottom: 1px solid #30363d; position: sticky; top: 0; white-space: nowrap; }
td { padding: 8px 10px; border-bottom: 1px solid #21262d; vertical-align: middle; }
tr:hover td { background: #161b22; }
.badge { display:inline-block; padding:2px 7px; border-radius:10px; font-size:10px; font-weight:600; }
.badge-a { background:#1f4a6e; color:#58a6ff; }
.badge-b { background:#2d1b1b; color:#f97316; }
.badge-c { background:#1b2d2d; color:#22d3ee; }
.badge-moon { background:#2d1b69; color:#a78bfa; }
.badge-up { background:#1a3a1a; color:#3fb950; }
.badge-flat { background:#2d2a1f; color:#d29922; }
.badge-down { background:#3a1a1a; color:#f85149; }
.badge-dead { background:#2a2a2a; color:#8b949e; }
.mult { font-weight:bold; }
.mult-moon { color:#a78bfa; }
.mult-up { color:#3fb950; }
.mult-flat { color:#d29922; }
.mult-down { color:#f85149; }
a { color:#58a6ff; text-decoration:none; }
a:hover { text-decoration:underline; }
.empty { text-align:center; color:#8b949e; padding:40px; }
.score-bar { display:inline-block; height:6px; border-radius:3px; background:#3fb950; vertical-align:middle; margin-right:4px; }
.nowrap { white-space: nowrap; }
</style>
</head>
<body>
<div class="header">
  <h1>🦞 PJM Scanner</h1>
  <div class="meta">{{ now }} UTC &nbsp;|&nbsp; Auto-refresh 60s &nbsp;|&nbsp; <a href="/health">health</a></div>
</div>

<div class="tabs">
  <div class="tab active"   onclick="showTab('a',    this)">🛡️ Safe (A)</div>
  <div class="tab"          onclick="showTab('b',    this)">⚡ Momentum (B)</div>
  <div class="tab"          onclick="showTab('c',    this)">🌊 Second Wave (C)</div>
  <div class="tab"          onclick="showTab('all',  this)">📊 All</div>
  <div class="tab"          onclick="showTab('top',  this)">🏆 Top Performers</div>
  <div class="tab"          onclick="showTab('wall', this)">🧠 Wallets</div>
</div>

{% for tab_id, tab_label, rows, stats in strategy_tabs %}
<div id="tab-{{ tab_id }}" class="content {% if loop.first %}active{% endif %}">
  <div class="cards">
    <div class="card"><div class="label">Detected</div><div class="value blue">{{ stats.total_detected or 0 }}</div></div>
    <div class="card"><div class="label">Outcomes</div><div class="value">{{ stats.total_outcomes or 0 }}</div></div>
    <div class="card"><div class="label">Avg Multiple</div>
      <div class="value {% if (stats.avg_multiple or 0) >= 2 %}green{% elif (stats.avg_multiple or 0) >= 1 %}yellow{% else %}red{% endif %}">
        {{ "%.2f"|format(stats.avg_multiple or 0) }}x
      </div>
    </div>
    <div class="card"><div class="label">🚀 Moon</div><div class="value purple">{{ stats.moon_count or 0 }}</div></div>
    <div class="card"><div class="label">📈 Up</div><div class="value green">{{ stats.up_count or 0 }}</div></div>
    <div class="card"><div class="label">📉 Down/Dead</div><div class="value red">{{ (stats.down_count or 0) + (stats.dead_count or 0) }}</div></div>
    <div class="card"><div class="label">2x+</div><div class="value green">{{ stats.count_2x or 0 }}</div></div>
    <div class="card"><div class="label">5x+</div><div class="value purple">{{ stats.count_5x or 0 }}</div></div>
    <div class="card"><div class="label">10x+</div><div class="value purple">{{ stats.count_10x or 0 }}</div></div>
    <div class="card"><div class="label">Entry Signals</div><div class="value yellow">{{ stats.entry_signals_sent or 0 }}</div></div>
  </div>

  {% if rows %}
  <div style="overflow-x:auto;">
  <table>
    <thead>
      <tr>
        <th>Token</th>
        <th>Strategy</th>
        <th>Score</th>
        <th>MC at Detection</th>
        <th>MC at Entry</th>
        <th>Current MC</th>
        <th>vs Detection</th>
        <th>vs Entry</th>
        <th>Vol 5m</th>
        <th>1h Chg</th>
        <th>B/S Ratio</th>
        <th>Entry Signal</th>
        <th>Outcome</th>
        <th>Detected</th>
        <th>Links</th>
      </tr>
    </thead>
    <tbody>
    {% for r in rows %}
    <tr>
      <td class="nowrap">
        <b>{{ r.symbol }}</b><br>
        <span style="color:#8b949e;font-size:10px">{{ (r.name or '')[:18] }}</span>
      </td>
      <td>
        <span class="badge badge-{{ r.strategy.lower() }}">{{ r.strategy }}</span>
      </td>
      <td class="nowrap">
        <span style="color:{% if r.score >= 80 %}#a78bfa{% elif r.score >= 65 %}#3fb950{% else %}#d29922{% endif %}">
          {{ "%.0f"|format(r.score or 0) }}
        </span>
        <div class="score-bar" style="width:{{ "%.0f"|format((r.score or 0) * 0.6) }}px;
          background:{% if r.score >= 80 %}#a78bfa{% elif r.score >= 65 %}#3fb950{% else %}#d29922{% endif %};"></div>
      </td>
      <td>${{ "{:,.0f}".format(r.mc_at_detection or 0) }}</td>
      <td>{% if r.entry_price %}${{ "{:,.0f}".format(r.mc_at_signal or 0) }}{% else %}—{% endif %}</td>
      <td>{% if r.current_mc %}${{ "{:,.0f}".format(r.current_mc) }}{% else %}<span style="color:#8b949e">stale</span>{% endif %}</td>
      <td>
        {% if r.multiple_vs_detection %}
        {% set m = r.multiple_vs_detection %}
        <span class="mult {% if m >= 5 %}mult-moon{% elif m >= 2 %}mult-up{% elif m >= 0.8 %}mult-flat{% else %}mult-down{% endif %}">
          {{ "%.2f"|format(m) }}x
        </span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.multiple_vs_entry %}
        <span class="mult {% if r.multiple_vs_entry >= 2 %}mult-up{% elif r.multiple_vs_entry >= 0.8 %}mult-flat{% else %}mult-down{% endif %}">
          {{ "%.2f"|format(r.multiple_vs_entry) }}x
        </span>
        {% else %}—{% endif %}
      </td>
      <td>{% if r.vol_5m %}${{ "{:,.0f}".format(r.vol_5m) }}{% else %}—{% endif %}</td>
      <td>
        {% if r.price_change_1h is not none %}
        <span style="color:{% if r.price_change_1h >= 0 %}#3fb950{% else %}#f85149{% endif %}">
          {{ "%+.1f"|format(r.price_change_1h) }}%
        </span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.buy_sell_ratio_1h %}
        <span style="color:{% if r.buy_sell_ratio_1h >= 1.3 %}#3fb950{% elif r.buy_sell_ratio_1h >= 1.0 %}#d29922{% else %}#f85149{% endif %}">
          {{ "%.2f"|format(r.buy_sell_ratio_1h) }}
        </span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.entry_sent_at %}
          <span style="color:#58a6ff;font-size:10px">
            {{ r.signal_type or 'pullback' }}<br>
            -{{ "%.1f"|format(r.pullback_pct or 0) }}% | ${{ "{:,.0f}".format(r.mc_at_signal or 0) }}
          </span>
        {% elif r.watching_pullback %}
          <span style="color:#8b949e;font-size:10px">👀 watching</span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if r.outcome %}
          <span class="badge badge-{{ r.outcome }}">{{ r.outcome }}</span>
        {% elif r.check_due_at %}
          <span style="color:#8b949e;font-size:10px">
            due {{ r.check_due_at.strftime('%m/%d %H:%M') }}
          </span>
        {% else %}—{% endif %}
      </td>
      <td>
        <span style="color:#8b949e;font-size:10px">
          {{ r.detected_at.strftime('%m/%d %H:%M') if r.detected_at else '—' }}
        </span>
      </td>
      <td class="nowrap">
        <a href="https://dexscreener.com/solana/{{ r.pair_addr }}" target="_blank">DEX</a>
        <a href="https://rugcheck.xyz/tokens/{{ r.mint }}" target="_blank">RC</a>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="empty">No detections yet for this strategy.</div>
  {% endif %}
</div>
{% endfor %}

<!-- Top Performers Tab -->
<div id="tab-top" class="content">
  <div class="cards">
    <div class="card"><div class="label">2x+ Tokens</div><div class="value green">{{ top_stats.count_2x or 0 }}</div></div>
    <div class="card"><div class="label">5x+ Tokens</div><div class="value purple">{{ top_stats.count_5x or 0 }}</div></div>
    <div class="card"><div class="label">10x+ Tokens</div><div class="value purple">{{ top_stats.count_10x or 0 }}</div></div>
    <div class="card"><div class="label">20x+ Tokens</div><div class="value purple">{{ top_stats.get('count_20x', 0) }}</div></div>
  </div>
  {% if top_performers %}
  <table>
    <thead>
      <tr>
        <th>#</th><th>Token</th><th>Strategy</th>
        <th>MC at Detection</th><th>Current MC</th>
        <th>vs Detection</th><th>vs Entry</th>
        <th>Outcome</th><th>Detected</th>
      </tr>
    </thead>
    <tbody>
    {% for i, r in top_performers | enumerate %}
    <tr>
      <td style="color:#8b949e">{{ i+1 }}</td>
      <td><b>{{ r.symbol }}</b><br><span style="color:#8b949e;font-size:10px">{{ (r.name or '')[:18] }}</span></td>
      <td><span class="badge badge-{{ r.strategy.lower() }}">{{ r.strategy }}</span></td>
      <td>${{ "{:,.0f}".format(r.mc_at_detection or 0) }}</td>
      <td>${{ "{:,.0f}".format(r.current_mc or 0) }}</td>
      <td>
        {% set m = r.multiple_vs_detection %}
        <span class="mult {% if m >= 5 %}mult-moon{% elif m >= 2 %}mult-up{% else %}mult-flat{% endif %}">
          {{ "%.2f"|format(m or 0) }}x
        </span>
      </td>
      <td>{% if r.multiple_vs_entry %}{{ "%.2f"|format(r.multiple_vs_entry) }}x{% else %}—{% endif %}</td>
      <td>{% if r.outcome %}<span class="badge badge-{{ r.outcome }}">{{ r.outcome }}</span>{% else %}—{% endif %}</td>
      <td style="color:#8b949e;font-size:10px">{{ r.detected_at.strftime('%m/%d') if r.detected_at else '—' }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No performers yet — keep scanning!</div>
  {% endif %}
</div>

<!-- Wallets Tab -->
<div id="tab-wall" class="content">
  {% if wallets %}
  <table>
    <thead>
      <tr>
        <th>Address</th><th>Score</th><th>Win Rate</th>
        <th>Avg ROI</th><th>Total Trades</th><th>Wins</th><th>Last Active</th>
      </tr>
    </thead>
    <tbody>
    {% for w in wallets %}
    <tr>
      <td><a href="https://solscan.io/address/{{ w.address }}" target="_blank">{{ w.address[:8] }}…{{ w.address[-4:] }}</a></td>
      <td><span style="color:{% if w.score >= 0.8 %}#a78bfa{% elif w.score >= 0.6 %}#3fb950{% else %}#d29922{% endif %}">{{ "%.3f"|format(w.score or 0) }}</span></td>
      <td>{{ "%.1f"|format((w.win_rate or 0) * 100) }}%</td>
      <td>{{ "%.2f"|format(w.avg_roi or 0) }}x</td>
      <td>{{ w.total_trades or 0 }}</td>
      <td>{{ w.winning_trades or 0 }}</td>
      <td style="color:#8b949e;font-size:10px">{{ w.last_active.strftime('%m/%d %H:%M') if w.last_active else '—' }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No tracked wallets yet.</div>
  {% endif %}
</div>

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


@app.route("/")
def index():
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    tabs = []
    for tab_id, strategy in [("a","A"), ("b","B"), ("c","C"), ("all", None)]:
        rows  = db.get_dashboard_tokens(strategy)
        stats = db.get_strategy_stats(strategy)
        tabs.append((tab_id, strategy or "All", rows, stats))

    top_performers = db.get_top_performers(limit=50, min_multiple=2.0)
    top_stats      = db.get_strategy_stats()

    wallets        = db.get_active_wallets(min_score=0.0)

    return render_template_string(
        HTML,
        now=now,
        strategy_tabs=tabs,
        top_performers=list(enumerate(top_performers)),
        top_stats=top_stats,
        wallets=wallets,
    )


@app.route("/health")
def health():
    try:
        from core.circuit_breaker import status_all
        return jsonify({"status": "ok", "circuits": status_all()})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/stats")
def api_stats():
    return jsonify({
        "A": db.get_strategy_stats("A"),
        "B": db.get_strategy_stats("B"),
        "C": db.get_strategy_stats("C"),
    })


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
