'use strict';

const API_BASE = '/api';

let currentUser = null;
let allTickets = [];
let filteredTickets = [];

function getAuthHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (currentUser && currentUser.access_token) {
    h['Authorization'] = `Bearer ${currentUser.access_token}`;
  }
  return h;
}

// ==================== UTILITIES ====================
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function toast(msg, type = 'info') {
  const tc = document.getElementById('toastContainer');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  tc.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

function statusBadge(status) {
  const map = {
    'open': 'badge-open', 'in progress': 'badge-inprogress',
    'resolved': 'badge-resolved', 'closed': 'badge-closed'
  };
  const cls = map[(status || '').toLowerCase()] || 'badge-closed';
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}

function priorityBadge(priority) {
  const map = {
    'critical': 'badge-critical', 'high': 'badge-high',
    'medium': 'badge-medium', 'low': 'badge-low'
  };
  const cls = map[(priority || '').toLowerCase()] || 'badge-low';
  return `<span class="badge ${cls}">${esc(priority)}</span>`;
}

function auditBadge(status) {
  const s = (status || '').toLowerCase();
  return `<span class="badge ${s === 'success' ? 'badge-success' : 'badge-failed'}">${esc(status)}</span>`;
}

// Live clock
function startClock() {
  const el = document.getElementById('topbarTime');
  function tick() {
    const now = new Date();
    el.textContent = now.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  tick();
  setInterval(tick, 1000);
}

// ==================== AUTH ====================
async function login(e) {
  e.preventDefault();
  const id = document.getElementById('loginId').value.trim();
  const pwd = document.getElementById('loginPwd').value;
  const errEl = document.getElementById('loginError');
  const btn = document.getElementById('loginSubmitBtn');
  const btnText = document.getElementById('loginBtnText');

  errEl.hidden = true;
  btn.disabled = true;
  btnText.textContent = 'Signing in…';

  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ employee_id: id, password: pwd, portal: 'it' })
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      errEl.textContent = err.detail || 'Invalid credentials or wrong portal. IT staff only.';
      errEl.hidden = false;
      return;
    }

    currentUser = await res.json();
    if (currentUser.role.toLowerCase() !== 'it') {
      errEl.textContent = 'Access denied. This portal is for IT staff only. Please use the Employee Portal.';
      errEl.hidden = false;
      currentUser = null;
      return;
    }

    showDashboard();
  } catch (err) {
    errEl.textContent = 'Cannot reach the backend server. Please ensure it is running on port 8000.';
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
    btnText.textContent = 'Sign in to Command Center';
  }
}

function showDashboard() {
  // Update sidebar user info
  const initials = currentUser.name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase();
  document.getElementById('sidebarAvatar').textContent = initials;
  document.getElementById('sidebarUserName').textContent = currentUser.name;

  document.getElementById('loginScreen').hidden = true;
  document.getElementById('dashboard').hidden = false;

  startClock();
  loadOverview();
}

function logout() {
  currentUser = null;
  allTickets = [];
  filteredTickets = [];
  document.getElementById('dashboard').hidden = true;
  document.getElementById('loginScreen').hidden = false;
  document.getElementById('loginPwd').value = '';
  document.getElementById('loginError').hidden = true;
}

// ==================== NAVIGATION ====================
const PAGE_CONFIG = {
  overview: { title: 'Command Center Overview', subtitle: 'Real-time IT incident monitoring and management', page: 'pageOverview' },
  tickets:  { title: 'Incident Tickets', subtitle: 'Manage, triage, and resolve all support tickets', page: 'pageTickets' },
  outages:  { title: 'Outage Monitor', subtitle: 'Check and track network outage status by location', page: 'pageOutages' },
  audit:    { title: 'Audit Trail', subtitle: 'System event logs and administrative actions', page: 'pageAudit' },
  guides:   { title: 'Guide Administration', subtitle: 'Inspect and rebuild the approved IT knowledge base index', page: 'pageGuides' },
};

function navigate(pageKey) {
  const cfg = PAGE_CONFIG[pageKey];
  if (!cfg) return;

  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  const navEl = document.querySelector(`.nav-item[data-page="${pageKey}"]`);
  if (navEl) navEl.classList.add('active');

  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
  document.getElementById(cfg.page).classList.add('active');

  document.getElementById('pageTitle').textContent = cfg.title;
  document.getElementById('pageSubtitle').textContent = cfg.subtitle;

  // Load page data
  if (pageKey === 'tickets') loadTickets();
  if (pageKey === 'overview') loadOverview();
  if (pageKey === 'outages') loadAllOutages();
  if (pageKey === 'audit') loadAuditLogs();
  if (pageKey === 'guides') { loadGuidesTable(); checkGuideStatus(); }
}

// ==================== OVERVIEW ====================
async function loadOverview() {
  try {
    const [mRes, tRes] = await Promise.all([
      fetch(`${API_BASE}/dashboard/metrics`, { headers: getAuthHeaders() }),
      fetch(`${API_BASE}/tickets`, { headers: getAuthHeaders() })
    ]);

    if (mRes.ok) {
      const m = await mRes.json();
      document.getElementById('metricTotal').textContent = m.total_tickets;
      document.getElementById('metricOpen').textContent = m.open_tickets;
      document.getElementById('metricInProgress').textContent = m.in_progress_tickets;
      document.getElementById('metricResolved').textContent = m.resolved_tickets;
      document.getElementById('metricOutages').textContent = m.active_outages || 0;

      // Update status pill
      if ((m.open_tickets || 0) > 5) {
        document.getElementById('statusPill').classList.add('warn');
        document.getElementById('statusText').textContent = 'High Ticket Volume';
      }

      renderServiceBreakdown(m.tickets_by_service || {});
      renderPriorityBreakdown(m.tickets_by_priority || {});
    }

    if (tRes.ok) {
      allTickets = await tRes.json();
      document.getElementById('navTicketBadge').textContent = allTickets.filter(t => t.status === 'Open').length || 0;
      renderRecentTickets(allTickets.slice(0, 6));
    }
  } catch (err) {
    console.error('Overview load error:', err);
    toast('Could not load dashboard data. Is the backend running?', 'error');
  }
}

function renderRecentTickets(tickets) {
  const el = document.getElementById('recentTicketsList');
  if (!tickets.length) {
    el.innerHTML = '<p class="muted-text">No tickets yet.</p>';
    return;
  }
  el.innerHTML = tickets.map(t => `
    <div class="recent-ticket-row">
      <span class="recent-ticket-id">${esc(t.ticket_id)}</span>
      <span class="recent-ticket-issue">${esc(t.issue)}</span>
      <div class="recent-ticket-meta">${statusBadge(t.status)}</div>
    </div>
  `).join('');
}

function makeBar(label, count, max, colorClass) {
  const pct = max > 0 ? Math.round((count / max) * 100) : 0;
  return `
    <div class="bar-item">
      <div class="bar-label-row">
        <span class="bar-label">${esc(label)}</span>
        <span class="bar-count">${count}</span>
      </div>
      <div class="bar-track"><div class="bar-fill ${colorClass}" style="width:${pct}%"></div></div>
    </div>
  `;
}

function renderServiceBreakdown(byService) {
  const el = document.getElementById('serviceBreakdown');
  const entries = Object.entries(byService).sort((a, b) => b[1] - a[1]);
  if (!entries.length) { el.innerHTML = '<p class="muted-text">No data yet.</p>'; return; }
  const max = entries[0][1];
  const colors = ['blue', 'amber', 'green', 'purple', 'red'];
  el.innerHTML = entries.map(([svc, cnt], i) => makeBar(svc, cnt, max, colors[i % colors.length])).join('');
}

function renderPriorityBreakdown(byPriority) {
  const el = document.getElementById('priorityBreakdown');
  const order = ['Critical', 'High', 'Medium', 'Low'];
  const colorMap = { Critical: 'red', High: 'amber', Medium: 'blue', Low: 'green' };
  const entries = order.filter(k => byPriority[k] != null).map(k => [k, byPriority[k]]);
  if (!entries.length) { el.innerHTML = '<p class="muted-text">No data yet.</p>'; return; }
  const max = Math.max(...entries.map(([, v]) => v));
  el.innerHTML = entries.map(([pri, cnt]) => makeBar(pri, cnt, max, colorMap[pri] || 'blue')).join('');
}

// ==================== TICKETS ====================
async function loadTickets() {
  document.getElementById('ticketsTableBody').innerHTML = '<tr><td colspan="10" class="table-loading">Loading tickets&hellip;</td></tr>';
  try {
    const res = await fetch(`${API_BASE}/tickets`, { headers: getAuthHeaders() });
    if (!res.ok) throw new Error('Failed to fetch tickets');
    allTickets = await res.json();
    filteredTickets = [...allTickets];
    applyFilters();
  } catch (err) {
    document.getElementById('ticketsTableBody').innerHTML = '<tr><td colspan="10" class="table-loading">Error loading tickets.</td></tr>';
    toast('Error loading tickets.', 'error');
  }
}

function applyFilters() {
  const q = (document.getElementById('ticketSearch').value || '').toLowerCase();
  const statusF = (document.getElementById('filterStatus').value || '').toLowerCase();
  const prioF = (document.getElementById('filterPriority').value || '').toLowerCase();
  const svcF = (document.getElementById('filterService').value || '').toLowerCase();

  filteredTickets = allTickets.filter(t => {
    const matchQ = !q || [t.ticket_id, t.issue, t.employee_id, t.location, t.service].some(f => (f || '').toLowerCase().includes(q));
    const matchStatus = !statusF || (t.status || '').toLowerCase() === statusF;
    const matchPrio = !prioF || (t.priority || '').toLowerCase() === prioF;
    const matchSvc = !svcF || (t.service || '').toLowerCase() === svcF;
    return matchQ && matchStatus && matchPrio && matchSvc;
  });
  renderTicketsTable();
}

function renderTicketsTable() {
  const tbody = document.getElementById('ticketsTableBody');
  document.getElementById('tableCount').textContent = `${filteredTickets.length} ticket${filteredTickets.length !== 1 ? 's' : ''}`;

  if (!filteredTickets.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="table-loading">No tickets match the current filters.</td></tr>';
    return;
  }

  tbody.innerHTML = filteredTickets.map(t => `
    <tr>
      <td class="ticket-id-cell">${esc(t.ticket_id)}</td>
      <td class="ticket-issue-cell" title="${esc(t.issue)}">${esc(t.issue)}</td>
      <td>${esc(t.employee_id)}</td>
      <td>${esc(t.location)}</td>
      <td>${esc(t.service)}</td>
      <td>${priorityBadge(t.priority)}</td>
      <td>${statusBadge(t.status)}</td>
      <td>${esc(t.assigned_technician || '—')}</td>
      <td style="font-size:11px;white-space:nowrap;">${esc((t.created_at || '').slice(0, 16))}</td>
      <td>
        <button class="btn-icon" title="Manage ticket" onclick="openTicketPanel('${esc(t.ticket_id)}')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </button>
      </td>
    </tr>
  `).join('');
}

// ==================== TICKET DETAIL PANEL ====================
window.openTicketPanel = async function(ticketId) {
  const overlay = document.getElementById('ticketPanelOverlay');
  const detailBody = document.getElementById('detailBody');
  document.getElementById('detailTicketId').textContent = ticketId;
  document.getElementById('detailStatusRow').innerHTML = '';
  detailBody.innerHTML = '<div class="detail-loading">Loading ticket details&hellip;</div>';
  overlay.hidden = false;

  try {
    const res = await fetch(`${API_BASE}/tickets/${ticketId}`, {
      headers: getAuthHeaders()
    });
    if (!res.ok) throw new Error('Not found or unauthorized');
    const t = await res.json();

    document.getElementById('detailStatusRow').innerHTML = `${statusBadge(t.status)} ${priorityBadge(t.priority)}`;

    const historyHtml = (t.history || []).length
      ? t.history.map(h => `
          <div class="history-item">
            <div class="history-ts">${esc(h.timestamp)}</div>
            <div><span class="history-by">${esc(h.performed_by)}</span> &mdash; ${esc(h.action)}</div>
            <div class="history-detail">${esc(h.details)}</div>
          </div>
        `).join('')
      : '<p class="muted-text">No history recorded yet.</p>';

    const stepsHtml = (t.attempted_steps || []).length
      ? t.attempted_steps.map(s => `<div class="step-item">${esc(s)}</div>`).join('')
      : '<p class="muted-text">No steps attempted.</p>';

    detailBody.innerHTML = `
      <!-- Info -->
      <div class="detail-section">
        <div class="detail-section-title">Incident Details</div>
        <div class="detail-kv">
          <span class="detail-key">Employee</span><span class="detail-val">${esc(t.employee_id)}</span>
          <span class="detail-key">Location</span><span class="detail-val">${esc(t.location)}</span>
          <span class="detail-key">Service</span><span class="detail-val">${esc(t.service)}</span>
          <span class="detail-key">Department</span><span class="detail-val">${esc(t.department)}</span>
          <span class="detail-key">Issue</span><span class="detail-val">${esc(t.issue)}</span>
          <span class="detail-key">Created</span><span class="detail-val">${esc(t.created_at)}</span>
          <span class="detail-key">Updated</span><span class="detail-val">${esc(t.updated_at)}</span>
          ${t.outage_ref ? `<span class="detail-key">Outage Ref</span><span class="detail-val">${esc(t.outage_ref)}</span>` : ''}
        </div>
      </div>

      <!-- Attempted Steps -->
      <div class="detail-section">
        <div class="detail-section-title">Troubleshooting Attempted</div>
        <div class="attempted-steps-list">${stepsHtml}</div>
      </div>

      <!-- Update Form -->
      <div class="detail-section">
        <div class="detail-section-title">Update Ticket</div>
        <form class="detail-form" id="updateTicketForm" data-id="${esc(t.ticket_id)}">
          <div>
            <label class="detail-form-label" for="updStatus">Status</label>
            <select id="updStatus" class="detail-form-select">
              ${['Open','In progress','Resolved','Closed'].map(s => `<option value="${s}" ${t.status === s ? 'selected' : ''}>${s}</option>`).join('')}
            </select>
          </div>
          <div>
            <label class="detail-form-label" for="updPriority">Priority</label>
            <select id="updPriority" class="detail-form-select">
              ${['Critical','High','Medium','Low'].map(p => `<option value="${p}" ${t.priority === p ? 'selected' : ''}>${p}</option>`).join('')}
            </select>
          </div>
          <div>
            <label class="detail-form-label" for="updTech">Assigned Technician</label>
            <input id="updTech" class="detail-form-input" type="text" value="${esc(t.assigned_technician || '')}" placeholder="e.g. IT1001, Tech-Priya">
          </div>
          <div>
            <label class="detail-form-label" for="updResolution">Resolution Notes</label>
            <textarea id="updResolution" class="detail-form-textarea" rows="3" placeholder="Describe what resolved the issue…">${esc(t.resolution_notes || '')}</textarea>
          </div>
          <div class="detail-actions">
            <button class="btn-primary" type="submit">Save Changes</button>
            <button class="btn-secondary" type="button" id="closePanelFromForm">Cancel</button>
          </div>
        </form>
      </div>

      <!-- History -->
      <div class="detail-section">
        <div class="detail-section-title">Audit &amp; History Trail</div>
        ${historyHtml}
      </div>
    `;

    document.getElementById('updateTicketForm').addEventListener('submit', submitTicketUpdate);
    document.getElementById('closePanelFromForm').addEventListener('click', closeTicketPanel);
  } catch (err) {
    detailBody.innerHTML = `<p class="muted-text">Error loading ticket: ${esc(err.message)}</p>`;
  }
};

async function submitTicketUpdate(e) {
  e.preventDefault();
  const ticketId = e.target.dataset.id;
  const payload = {
    status: document.getElementById('updStatus').value,
    priority: document.getElementById('updPriority').value,
    assigned_technician: document.getElementById('updTech').value.trim(),
    resolution_notes: document.getElementById('updResolution').value.trim()
  };

  try {
    const res = await fetch(`${API_BASE}/tickets/${ticketId}`, {
      method: 'PATCH',
      headers: getAuthHeaders(),
      body: JSON.stringify(payload)
    });

    if (res.ok) {
      toast(`Ticket ${ticketId} updated successfully.`, 'success');
      closeTicketPanel();
      // Refresh ticket list or overview
      if (document.getElementById('pageTickets').classList.contains('active')) loadTickets();
      else loadOverview();
    } else {
      const err = await res.json().catch(() => ({}));
      toast(err.detail || 'Update failed.', 'error');
    }
  } catch (err) {
    toast('Network error: ' + err.message, 'error');
  }
}

function closeTicketPanel() {
  document.getElementById('ticketPanelOverlay').hidden = true;
}

// ==================== OUTAGE MONITOR & MANAGEMENT ====================
let allOutages = [];

async function loadAllOutages() {
  const tbody = document.getElementById('outagesTableBody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="7" class="table-loading">Loading outages&hellip;</td></tr>';
  try {
    const res = await fetch(`${API_BASE}/outages/all`);
    if (!res.ok) throw new Error('Failed to load outages');
    allOutages = await res.json();
    if (!allOutages.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-loading">No outage records found.</td></tr>';
      return;
    }
    tbody.innerHTML = allOutages.map(o => `
      <tr>
        <td style="font-weight:700;color:#388bfd;font-size:12px;">${esc(o.outage_ref)}</td>
        <td>${esc(o.location)}</td>
        <td>${esc(o.service)}</td>
        <td>${statusBadge(o.status)}${o.is_stale ? ' <span class="badge badge-low" style="color:#d29922;">Stale</span>' : ''}</td>
        <td style="font-size:11px;white-space:nowrap;">${esc(o.last_updated || '—')}</td>
        <td style="font-size:12px;color:#c9d1d9;max-width:240px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc(o.update_text)}">${esc(o.update_text)}</td>
        <td style="white-space:nowrap;">
          <button class="btn-secondary small" style="margin-right:6px;" onclick="editOutage('${esc(o.outage_ref)}')">Edit</button>
          <button class="btn-primary small" onclick="toggleOutage('${esc(o.outage_ref)}')">${o.status === 'Active' ? 'Mark Resolved' : 'Mark Active'}</button>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="7" class="table-loading">Error loading outages: ${esc(err.message)}</td></tr>`;
  }
}

window.editOutage = function(ref) {
  const o = allOutages.find(x => x.outage_ref === ref);
  if (!o) return;
  document.getElementById('outageFormPanel').hidden = false;
  document.getElementById('outageFormTitle').textContent = `Update Outage: ${o.outage_ref}`;
  document.getElementById('formOutageRef').value = o.outage_ref;
  document.getElementById('formOutageLocation').value = o.location;
  document.getElementById('formOutageService').value = o.service;
  document.getElementById('formOutageStatus').value = o.status;
  document.getElementById('formOutageText').value = o.update_text;
  document.getElementById('outageFormPanel').scrollIntoView({ behavior: 'smooth' });
};

window.toggleOutage = async function(ref) {
  try {
    const res = await fetch(`${API_BASE}/it/outages/${encodeURIComponent(ref)}/toggle`, {
      method: 'POST',
      headers: getAuthHeaders()
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to toggle outage status');
    }
    const updated = await res.json();
    toast(`Outage ${ref} status toggled to ${updated.status}.`, 'success');
    loadAllOutages();
    loadOverview();
  } catch (err) {
    toast(err.message, 'error');
  }
};

function openNewOutage() {
  document.getElementById('outageFormPanel').hidden = false;
  document.getElementById('outageFormTitle').textContent = 'Record New Outage';
  document.getElementById('formOutageRef').value = '';
  document.getElementById('formOutageLocation').value = '';
  document.getElementById('formOutageService').value = 'Wi-Fi';
  document.getElementById('formOutageStatus').value = 'Active';
  document.getElementById('formOutageText').value = '';
  document.getElementById('outageFormPanel').scrollIntoView({ behavior: 'smooth' });
}

function closeOutageForm() {
  document.getElementById('outageFormPanel').hidden = true;
}

async function submitOutageForm(e) {
  e.preventDefault();
  const btn = document.getElementById('saveOutageBtn');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  const payload = {
    outage_ref: document.getElementById('formOutageRef').value.trim() || null,
    location: document.getElementById('formOutageLocation').value,
    service: document.getElementById('formOutageService').value,
    status: document.getElementById('formOutageStatus').value,
    update_text: document.getElementById('formOutageText').value.trim()
  };

  try {
    const res = await fetch(`${API_BASE}/it/outages`, {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to save outage');
    }
    const saved = await res.json();
    toast(`Outage ${saved.outage_ref} saved successfully.`, 'success');
    closeOutageForm();
    loadAllOutages();
    loadOverview();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save Outage Record';
  }
}

async function checkOutage() {
  const location = document.getElementById('outageLocation').value;
  const service = document.getElementById('outageService').value;
  const resultEl = document.getElementById('outageResult');
  const historyEl = document.getElementById('outageHistory');

  if (!location) { toast('Please select a location.', 'info'); return; }

  resultEl.hidden = true;
  resultEl.className = 'outage-result';
  historyEl.innerHTML = '<p class="muted-text">Loading&hellip;</p>';

  try {
    const [outageRes, histRes] = await Promise.all([
      fetch(`${API_BASE}/outages?location=${encodeURIComponent(location)}&service=${encodeURIComponent(service)}`),
      fetch(`${API_BASE}/history?location=${encodeURIComponent(location)}&service=${encodeURIComponent(service)}`)
    ]);

    if (outageRes.ok) {
      const o = await outageRes.json();
      const statusClass = o.status === 'Active' ? 'outage-active' : o.status === 'Resolved' ? 'outage-resolved' : 'outage-unknown';
      resultEl.className = `outage-result ${statusClass}`;
      resultEl.innerHTML = `
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
          <div class="outage-title">${esc(o.service)} — ${esc(o.location)}</div>
          ${statusBadge(o.status)}
        </div>
        <div class="outage-meta">Outage Ref: ${esc(o.outage_ref || '—')} &middot; Last updated: ${esc(o.last_updated || '—')}${o.is_stale ? ' <b>(stale)</b>' : ''}</div>
        <div class="outage-desc">${esc(o.message)}</div>
        ${o.update_text ? `<div class="outage-desc" style="margin-top:8px;color:#8b949e;">${esc(o.update_text)}</div>` : ''}
      `;
      resultEl.hidden = false;
    }

    if (histRes.ok) {
      const history = await histRes.json();
      historyEl.innerHTML = history.length
        ? history.map(h => `
            <div class="recent-ticket-row">
              <span class="recent-ticket-id">${esc(h.incident_id)}</span>
              <div style="flex:1;min-width:0;">
                <div style="font-size:13px;color:#c9d1d9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(h.symptom)}</div>
                <div style="font-size:11px;color:#8b949e;margin-top:2px;">${esc(h.resolution)} &middot; ${esc(h.created_at)}</div>
              </div>
              ${statusBadge(h.status)}
            </div>
          `).join('')
        : '<p class="muted-text">No historical records found for this location and service.</p>';
    }
  } catch (err) {
    toast('Error checking outage: ' + err.message, 'error');
  }
}

// ==================== AUDIT LOGS ====================
async function loadAuditLogs() {
  document.getElementById('auditTableBody').innerHTML = '<tr><td colspan="7" class="table-loading">Loading audit records&hellip;</td></tr>';
  try {
    const res = await fetch(`${API_BASE}/audit-logs`, {
      headers: getAuthHeaders()
    });
    if (!res.ok) throw new Error('Access denied or server error');
    const logs = await res.json();
    document.getElementById('auditTableBody').innerHTML = logs.length
      ? logs.map(l => `
          <tr>
            <td style="color:#484f58;font-size:11px;">${l.id}</td>
            <td style="font-size:11px;white-space:nowrap;">${esc((l.timestamp || '').slice(0, 19))}</td>
            <td style="font-weight:600;color:#388bfd;">${esc(l.user_id)}</td>
            <td style="text-transform:capitalize;">${esc(l.action)}</td>
            <td>${esc(l.target)}</td>
            <td>${auditBadge(l.result)}</td>
            <td style="font-size:12px;color:#8b949e;max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc(l.details)}">${esc(l.details)}</td>
          </tr>
        `).join('')
      : '<tr><td colspan="7" class="table-loading">No audit records found.</td></tr>';
  } catch (err) {
    document.getElementById('auditTableBody').innerHTML = `<tr><td colspan="7" class="table-loading">Error: ${esc(err.message)}</td></tr>`;
    toast('Error loading audit logs.', 'error');
  }
}

// ==================== GUIDE ADMIN & REGISTRY ====================
let allGuides = [];

async function loadGuidesTable() {
  const tbody = document.getElementById('guidesTableBody');
  const fileSelect = document.getElementById('formGuideFile');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="8" class="table-loading">Loading guide registry&hellip;</td></tr>';
  try {
    const res = await fetch(`${API_BASE}/guides`, { headers: getAuthHeaders() });
    if (!res.ok) throw new Error('Failed to fetch guides');
    const data = await res.json();
    allGuides = data.registered_guides || [];
    
    // Populate file dropdown
    if (fileSelect && data.available_files) {
      fileSelect.innerHTML = '<option value="">Select Guide File</option>' +
        data.available_files.map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
    }

    if (!allGuides.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="table-loading">No registered guides found.</td></tr>';
      return;
    }

    tbody.innerHTML = allGuides.map(g => {
      const isActive = g.status === 'Active';
      return `
        <tr>
          <td style="font-weight:700;color:#388bfd;font-size:12px;">${esc(g.id)}</td>
          <td style="font-weight:500;">${esc(g.name || '—')}</td>
          <td style="font-size:12px;color:#c9d1d9;"><code>${esc(g.filename)}</code></td>
          <td><span class="badge badge-medium">v${esc(g.version)}</span></td>
          <td style="font-size:12px;">${(g.services || []).join(', ') || 'All'}</td>
          <td>${statusBadge(g.status)}</td>
          <td style="font-size:12px;color:#8b949e;">${esc(g.approved_by || '—')}</td>
          <td style="white-space:nowrap;">
            ${!isActive ? `<button class="btn-primary small" style="margin-right:6px;" onclick="activateGuide('${esc(g.id)}')">Activate</button>` : '<span class="badge badge-success" style="margin-right:6px;">Current</span>'}
            <button class="btn-secondary small" onclick="editGuide('${esc(g.id)}')">Update</button>
          </td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="table-loading">Error loading guides: ${esc(err.message)}</td></tr>`;
  }
}

window.activateGuide = async function(guideId) {
  try {
    const res = await fetch(`${API_BASE}/guides/${encodeURIComponent(guideId)}/activate`, {
      method: 'POST',
      headers: getAuthHeaders()
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Activation failed');
    }
    toast(`Guide ${guideId} is now active and indexed!`, 'success');
    loadGuidesTable();
    checkGuideStatus();
  } catch (err) {
    toast(err.message, 'error');
  }
};

window.editGuide = function(guideId) {
  const g = allGuides.find(x => x.id === guideId);
  if (!g) return;
  document.getElementById('guideFormPanel').hidden = false;
  document.getElementById('guideFormTitle').textContent = `Update & Approve Guide: ${g.id}`;
  document.getElementById('formGuideId').value = g.id;
  document.getElementById('formGuideName').value = g.name || '';
  document.getElementById('formGuideVersion').value = g.version || '';
  document.getElementById('formGuideFile').value = g.filename || '';
  document.getElementById('guideFormPanel').scrollIntoView({ behavior: 'smooth' });
};

function openNewGuide() {
  document.getElementById('guideFormPanel').hidden = false;
  document.getElementById('guideFormTitle').textContent = 'Approve & Index New Guide';
  document.getElementById('formGuideId').value = '';
  document.getElementById('formGuideName').value = '';
  document.getElementById('formGuideVersion').value = '1.0';
  document.getElementById('formGuideFile').value = '';
  document.getElementById('guideFormPanel').scrollIntoView({ behavior: 'smooth' });
}

function closeGuideForm() {
  document.getElementById('guideFormPanel').hidden = true;
}

async function submitGuideForm(e) {
  e.preventDefault();
  const btn = document.getElementById('saveGuideBtn');
  btn.disabled = true;
  btn.textContent = 'Indexing…';

  const payload = {
    guide_id: document.getElementById('formGuideId').value.trim(),
    name: document.getElementById('formGuideName').value.trim(),
    version: document.getElementById('formGuideVersion').value.trim(),
    filename: document.getElementById('formGuideFile').value,
    supported_os: [document.getElementById('formGuideOS').value],
    services: ["Wi-Fi", "Printer", "Email"]
  };

  try {
    const res = await fetch(`${API_BASE}/guides/approve`, {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Guide approval failed');
    }
    const result = await res.json();
    toast(`Guide '${payload.guide_id}' approved & indexed (${result.indexed_chunks} chunks)!`, 'success');
    closeGuideForm();
    loadGuidesTable();
    checkGuideStatus();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Approve & Activate Guide';
  }
}

async function checkGuideStatus() {
  const el = document.getElementById('guideStatus');
  el.innerHTML = '<p class="muted-text">Loading&hellip;</p>';
  try {
    const res = await fetch(`${API_BASE}/guides`, {
      headers: getAuthHeaders()
    });
    if (!res.ok) throw new Error('Failed to fetch guide status');
    const d = await res.json();
    const info = d.approved_info || {};
    el.innerHTML = `
      <div class="detail-kv">
        <span class="detail-key">Total Chunks</span><span class="detail-val" style="font-weight:700;color:#3fb950;">${d.total_chunks || 0}</span>
        <span class="detail-key">Active ID</span><span class="detail-val">${esc(d.active_guide_id || '—')} (v${esc(d.active_guide_version || '—')})</span>
        <span class="detail-key">Source File</span><span class="detail-val"><code>${esc(d.source_filename || '—')}</code></span>
        <span class="detail-key">Topics</span><span class="detail-val">${(info.topics || []).map(t => `<span class="badge badge-inprogress" style="margin:1px;">${esc(t)}</span>`).join(' ') || '—'}</span>
        <span class="detail-key">Status</span><span class="detail-val"><span class="badge badge-resolved">Active</span></span>
      </div>
    `;
  } catch (err) {
    el.innerHTML = `<p class="muted-text">Error: ${esc(err.message)}</p>`;
  }
}

async function reindexGuides() {
  const btn = document.getElementById('reindexBtn');
  const resultEl = document.getElementById('reindexResult');
  btn.disabled = true;
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="animation:spin 0.8s linear infinite"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Re-indexing…';

  try {
    const res = await fetch(`${API_BASE}/guides/reindex`, {
      method: 'POST',
      headers: getAuthHeaders()
    });
    if (!res.ok) throw new Error('Re-index failed');
    const d = await res.json();
    resultEl.innerHTML = `Guide index rebuilt successfully — <b>${d.indexed_chunks} chunks</b> indexed from: ${esc((d.source_files || []).join(', '))}.`;
    resultEl.hidden = false;
    toast('Guide index rebuilt successfully!', 'success');
    loadGuidesTable();
    checkGuideStatus();
  } catch (err) {
    toast('Re-index failed: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Re-Index Guides Now';
  }
}

// ==================== GLOBAL REFRESH ====================
async function globalRefresh() {
  const btn = document.getElementById('globalRefreshBtn');
  btn.classList.add('spinning');
  const activePage = document.querySelector('.page.active');
  if (activePage) {
    const id = activePage.id;
    if (id === 'pageOverview') await loadOverview();
    else if (id === 'pageTickets') await loadTickets();
    else if (id === 'pageOutages') await loadAllOutages();
    else if (id === 'pageAudit') await loadAuditLogs();
    else if (id === 'pageGuides') { await loadGuidesTable(); await checkGuideStatus(); }
  }
  toast('Refreshed.', 'success');
  setTimeout(() => btn.classList.remove('spinning'), 600);
}

// ==================== EVENT WIRING ====================
document.addEventListener('DOMContentLoaded', () => {
  // Login
  document.getElementById('loginForm').addEventListener('submit', login);

  // Sidebar navigation
  document.querySelectorAll('.nav-item[data-page]').forEach(el => {
    el.addEventListener('click', e => {
      e.preventDefault();
      navigate(el.dataset.page);
    });
  });

  // "View all" panel actions
  document.querySelectorAll('.panel-action[data-page]').forEach(el => {
    el.addEventListener('click', () => navigate(el.dataset.page));
  });

  // Sidebar toggle
  const sidebar = document.getElementById('sidebar');
  document.getElementById('sidebarToggle').addEventListener('click', () => {
    if (window.innerWidth <= 860) {
      sidebar.classList.toggle('mobile-open');
    } else {
      sidebar.classList.toggle('collapsed');
    }
  });

  // Logout
  document.getElementById('logoutBtn').addEventListener('click', logout);

  // Global refresh
  document.getElementById('globalRefreshBtn').addEventListener('click', globalRefresh);

  // Ticket filters
  document.getElementById('ticketSearch').addEventListener('input', applyFilters);
  document.getElementById('filterStatus').addEventListener('change', applyFilters);
  document.getElementById('filterPriority').addEventListener('change', applyFilters);
  document.getElementById('filterService').addEventListener('change', applyFilters);
  document.getElementById('clearFiltersBtn').addEventListener('click', () => {
    document.getElementById('ticketSearch').value = '';
    document.getElementById('filterStatus').value = '';
    document.getElementById('filterPriority').value = '';
    document.getElementById('filterService').value = '';
    applyFilters();
  });

  // Close ticket panel
  document.getElementById('closePanelBtn').addEventListener('click', closeTicketPanel);
  document.getElementById('ticketPanelOverlay').addEventListener('click', e => {
    if (e.target === document.getElementById('ticketPanelOverlay')) closeTicketPanel();
  });

  // Outage monitor & management
  document.getElementById('checkOutageBtn').addEventListener('click', checkOutage);
  const openNewOutageBtn = document.getElementById('openNewOutageBtn');
  if (openNewOutageBtn) openNewOutageBtn.addEventListener('click', openNewOutage);
  const cancelOutageFormBtn = document.getElementById('cancelOutageFormBtn');
  if (cancelOutageFormBtn) cancelOutageFormBtn.addEventListener('click', closeOutageForm);
  const closeOutageFormBtn = document.getElementById('closeOutageFormBtn');
  if (closeOutageFormBtn) closeOutageFormBtn.addEventListener('click', closeOutageForm);
  const outageUpsertForm = document.getElementById('outageUpsertForm');
  if (outageUpsertForm) outageUpsertForm.addEventListener('submit', submitOutageForm);

  // Audit logs
  document.getElementById('loadAuditBtn').addEventListener('click', loadAuditLogs);

  // Guide admin & management
  document.getElementById('checkGuidesBtn').addEventListener('click', checkGuideStatus);
  document.getElementById('reindexBtn').addEventListener('click', reindexGuides);
  const openNewGuideBtn = document.getElementById('openNewGuideBtn');
  if (openNewGuideBtn) openNewGuideBtn.addEventListener('click', openNewGuide);
  const cancelGuideFormBtn = document.getElementById('cancelGuideFormBtn');
  if (cancelGuideFormBtn) cancelGuideFormBtn.addEventListener('click', closeGuideForm);
  const closeGuideFormBtn = document.getElementById('closeGuideFormBtn');
  if (closeGuideFormBtn) closeGuideFormBtn.addEventListener('click', closeGuideForm);
  const guideApproveForm = document.getElementById('guideApproveForm');
  if (guideApproveForm) guideApproveForm.addEventListener('submit', submitGuideForm);

  // Keyboard: Escape closes panel
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      closeTicketPanel();
      closeOutageForm();
      closeGuideForm();
    }
  });

  // ── Auto-login: pick up user passed from employee portal via sessionStorage ──
  const stored = sessionStorage.getItem('it_portal_user');
  if (stored) {
    try {
      const user = JSON.parse(stored);
      if (user && user.role && user.role.toLowerCase() === 'it') {
        sessionStorage.removeItem('it_portal_user');
        currentUser = user;
        showDashboard();
        return;
      }
    } catch (_) {}
    sessionStorage.removeItem('it_portal_user');
  }
});
