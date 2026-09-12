"""Web UI: phase visualization + state panel + usage dashboard.

Served as a static HTML page from the FastAPI server at /dashboard.
Fetches live data from /status, /usage, and /usage/history endpoints.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

dashboard_app = FastAPI()

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AgentAlloy Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
         background: #0d1117; color: #c9d1d9; padding: 20px; }
  h1 { color: #58a6ff; margin-bottom: 20px; font-size: 1.5rem; }
  h2 { color: #8b949e; font-size: 1rem; margin-bottom: 12px; text-transform: uppercase;
       letter-spacing: 0.05em; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
          gap: 16px; margin-bottom: 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .phase-bar { display: flex; gap: 4px; margin: 12px 0; }
  .phase-step { flex: 1; padding: 8px 4px; text-align: center; border-radius: 4px;
                font-size: 0.75rem; font-weight: 600; background: #21262d; color: #484f58;
                transition: all 0.3s; }
  .phase-step.active { background: #1f6feb; color: #fff; }
  .phase-step.done { background: #238636; color: #fff; }
  .stat { display: flex; justify-content: space-between; padding: 8px 0;
          border-bottom: 1px solid #21262d; }
  .stat:last-child { border-bottom: none; }
  .stat-label { color: #8b949e; }
  .stat-value { color: #58a6ff; font-weight: 600; font-variant-numeric: tabular-nums; }
  .token-bar { height: 8px; background: #21262d; border-radius: 4px; margin-top: 8px;
               overflow: hidden; }
  .token-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
  .token-fill.injected { background: #f0883e; }
  .token-fill.user { background: #58a6ff; }
  .legend { display: flex; gap: 16px; margin-top: 8px; font-size: 0.75rem; }
  .legend-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block;
                margin-right: 4px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: left; color: #8b949e; padding: 8px; border-bottom: 1px solid #30363d; }
  td { padding: 8px; border-bottom: 1px solid #21262d; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.7rem;
           font-weight: 600; }
  .badge-ok { background: #238636; color: #fff; }
  .badge-warn { background: #9e6a03; color: #fff; }
  #refresh { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 12px;
             border-radius: 6px; cursor: pointer; font-size: 0.8rem; float: right; }
  #refresh:hover { background: #30363d; }
</style>
</head>
<body>
  <h1>AgentAlloy Dashboard <button id="refresh" onclick="load()">Refresh</button></h1>

  <div class="grid">
    <!-- Phase Progress -->
    <div class="card">
      <h2>SDD Phase</h2>
      <div class="phase-bar" id="phase-bar"></div>
      <div id="phase-detail" style="margin-top: 8px; font-size: 0.85rem;"></div>
    </div>

    <!-- Index Stats -->
    <div class="card">
      <h2>Code Index</h2>
      <div class="stat"><span class="stat-label">Symbols</span>
        <span class="stat-value" id="symbols">—</span></div>
      <div class="stat"><span class="stat-label">Chunks</span>
        <span class="stat-value" id="chunks">—</span></div>
      <div class="stat"><span class="stat-label">Search Mode</span>
        <span class="stat-value" id="search-mode">—</span></div>
    </div>

    <!-- Token Usage -->
    <div class="card">
      <h2>Token Usage</h2>
      <div class="stat"><span class="stat-label">Total Requests</span>
        <span class="stat-value" id="total-requests">—</span></div>
      <div class="stat"><span class="stat-label">Prompt Tokens</span>
        <span class="stat-value" id="prompt-tokens">—</span></div>
      <div class="stat"><span class="stat-label">Completion Tokens</span>
        <span class="stat-value" id="completion-tokens">—</span></div>
      <div class="stat"><span class="stat-label">Injected (AgentAlloy)</span>
        <span class="stat-value" id="injected-tokens">—</span></div>
      <div class="token-bar">
        <div class="token-fill user" id="user-bar" style="width:0%"></div>
      </div>
      <div class="token-bar" style="margin-top:4px">
        <div class="token-fill injected" id="injected-bar" style="width:0%"></div>
      </div>
      <div class="legend">
        <span><span class="legend-dot" style="background:#58a6ff"></span>User tokens</span>
        <span><span class="legend-dot" style="background:#f0883e"></span>Injected tokens</span>
      </div>
    </div>
  </div>

  <!-- Request History -->
  <div class="card">
    <h2>Recent Requests</h2>
    <table>
      <thead><tr><th>Time</th><th>Prompt</th><th>Completion</th><th>Injected</th><th>Status</th></tr></thead>
      <tbody id="history"></tbody>
    </table>
  </div>

  <div class="grid" style="margin-top: 16px;">
    <!-- Approval Gates -->
    <div class="card">
      <h2>Approval Gates</h2>
      <div id="gates-list"></div>
    </div>

    <!-- Sessions -->
    <div class="card">
      <h2>Sessions</h2>
      <table>
        <thead><tr><th>Key</th><th>Status</th><th>Phase</th></tr></thead>
        <tbody id="sessions-list"></tbody>
      </table>
    </div>

    <!-- Analytics -->
    <div class="card">
      <h2>Analytics</h2>
      <div class="stat"><span class="stat-label">Error Rate</span>
        <span class="stat-value" id="error-rate">—</span></div>
      <div class="stat"><span class="stat-label">Total Tool Calls</span>
        <span class="stat-value" id="total-tool-calls">—</span></div>
      <div class="stat"><span class="stat-label">Skills Loaded</span>
        <span class="stat-value" id="skills-loaded">—</span></div>
      <div id="top-tools" style="margin-top: 8px; font-size: 0.8rem;"></div>
    </div>
  </div>

<script>
const PHASES = ['spec', 'design', 'plan', 'build', 'qa', 'ship'];

async function load() {
  try {
    const [statusResp, usageResp, historyResp, gatesResp, sessionsResp, analyticsResp] =
      await Promise.all([
        fetch('/status'), fetch('/usage'), fetch('/usage/history'),
        fetch('/gates'), fetch('/sessions'), fetch('/analytics')
      ]);
    const status = await statusResp.json();
    const usage = await usageResp.json();
    const history = await historyResp.json();
    const gates = await gatesResp.json();
    const sessions = await sessionsResp.json();
    const analytics = await analyticsResp.json();

    // Phase bar
    const currentIdx = PHASES.indexOf(status.phase);
    const bar = document.getElementById('phase-bar');
    bar.innerHTML = PHASES.map((p, i) => {
      const cls = i < currentIdx ? 'done' : i === currentIdx ? 'active' : '';
      return `<div class="phase-step ${cls}">${p}</div>`;
    }).join('');
    document.getElementById('phase-detail').textContent =
      `Current: ${status.phase} | Model: :${status.model_port}`;

    // Index stats
    document.getElementById('symbols').textContent = (status.symbols || 0).toLocaleString();
    document.getElementById('chunks').textContent = (status.chunks || 0).toLocaleString();
    document.getElementById('search-mode').textContent =
      (status.symbols > 0 && status.chunks > 0) ? 'hybrid' : 'lexical-only';

    // Token usage
    document.getElementById('total-requests').textContent = usage.total_requests;
    document.getElementById('prompt-tokens').textContent =
      (usage.prompt_tokens || 0).toLocaleString();
    document.getElementById('completion-tokens').textContent =
      (usage.completion_tokens || 0).toLocaleString();
    document.getElementById('injected-tokens').textContent =
      (usage.injected_tokens || 0).toLocaleString();

    const total = (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);
    if (total > 0) {
      const userPct = ((usage.prompt_tokens - usage.injected_tokens) / total * 100);
      const injPct = (usage.injected_tokens / total * 100);
      document.getElementById('user-bar').style.width = userPct + '%';
      document.getElementById('injected-bar').style.width = injPct + '%';
    }

    // History
    const tbody = document.getElementById('history');
    const rows = (history.history || []).slice(0, 20);
    tbody.innerHTML = rows.map(r => {
      const time = new Date(r.timestamp).toLocaleTimeString();
      const badge = r.injected_tokens > 0
        ? `<span class="badge badge-warn">+${r.injected_tokens}</span>`
        : `<span class="badge badge-ok">0</span>`;
      return `<tr>
        <td>${time}</td>
        <td>${r.prompt_tokens}</td>
        <td>${r.completion_tokens}</td>
        <td>${r.injected_tokens}</td>
        <td>${badge}</td>
      </tr>`;
    }).join('');

    // Gates
    const gatesList = document.getElementById('gates-list');
    const gateRows = (gates.gates || []).filter(g => g.requires_approval !== false);
    gatesList.innerHTML = gateRows.map(g => {
      const icon = g.approved ? '✅' : g.has_exit_artifact ? '🟡' : '⬜';
      const lbl = g.has_exit_artifact
        ? (g.approved ? 'approved' : 'needs approval')
        : 'no artifact';
      return `<div class="stat">
        <span class="stat-label">${icon} ${g.phase} → next</span>
        <span class="stat-value" style="font-size:0.75rem">${lbl}</span>
      </div>`;
    }).join('') || '<div style="color:#484f58;font-size:0.85rem">No gated transitions</div>';

    // Sessions
    const sessList = document.getElementById('sessions-list');
    const sessRows = (sessions.sessions || []).slice(0, 10);
    sessList.innerHTML = sessRows.map(s => {
      const cls = s.status === 'active' ? 'badge-ok'
        : s.status === 'stashed' ? 'badge-warn' : '';
      return `<tr>
        <td>${s.session_key}</td>
        <td><span class="badge ${cls}">${s.status}</span></td>
        <td>${s.phase}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="3" style="color:#484f58">No sessions</td></tr>';

    // Analytics
    const errRate = analytics.error_rate || {};
    document.getElementById('error-rate').textContent =
      ((errRate.error_rate || 0) * 100).toFixed(1) + '%';
    const toolUsage = analytics.tool_usage || {};
    document.getElementById('total-tool-calls').textContent =
      (toolUsage.total_calls || 0).toLocaleString();
    document.getElementById('skills-loaded').textContent =
      (status.skills || 0).toLocaleString();

    const topTools = document.getElementById('top-tools');
    const tools = (toolUsage.tools || []).slice(0, 5);
    topTools.innerHTML = tools.length > 0
      ? '<strong style="color:#8b949e">Top tools:</strong> ' +
        tools.map(t => `${t.name} (${t.count})`).join(', ')
      : '';

  } catch (e) {
    console.error('Dashboard load failed:', e);
  }
}

load();
setInterval(load, 10000);
</script>
</body>
</html>"""


@dashboard_app.get("/dashboard")
def dashboard() -> HTMLResponse:
    """Serve the dashboard HTML."""
    return HTMLResponse(content=DASHBOARD_HTML)
