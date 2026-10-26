const $ = (s) => document.querySelector(s);
const chatBody = $('#chatBody'), input = $('#messageInput'), checklist = $('#checklist');
const modalBackdrop = $('#modalBackdrop'), modalContent = $('#modalContent');

const API_BASE = (window.location.port === '8000') ? '/api' : 'http://localhost:8000/api';

let currentUser = null;
let attempted = [];
let currentIssue = '';
let currentService = '';
let tickets = [];

function getAuthHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (currentUser && currentUser.access_token) {
    h['Authorization'] = `Bearer ${currentUser.access_token}`;
  }
  return h;
}

let conversationId = localStorage.getItem('resolveit_cid') || ('cid-' + Date.now() + '-' + Math.random().toString(36).substring(2, 8));
localStorage.setItem('resolveit_cid', conversationId);

// Stable guest session ID — persisted in sessionStorage for this browser tab.
// Used to scope guest tickets so different guests cannot see each other's tickets.
let guestSessionId = sessionStorage.getItem('resolveit_gsid');
if (!guestSessionId) {
  guestSessionId = 'gsid-' + Date.now() + '-' + Math.random().toString(36).substring(2, 9);
  sessionStorage.setItem('resolveit_gsid', guestSessionId);
}

// Idempotency guard: prevent duplicate sends
let _sendInProgress = false;

function addMessage(text, who = 'agent', extra = null) {
  const wrap = document.createElement('div');
  wrap.className = 'message ' + (who === 'user' ? 'user-message' : 'agent-message');
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = (who === 'user' ? 'You' : 'IT Support Agent') + ' · now';
  const p = document.createElement('p');
  p.textContent = text;
  wrap.append(label, p);

  if (extra && extra.length) {
    const area = document.createElement('div');
    area.className = 'suggestions';
    extra.forEach(item => {
      const b = document.createElement('button');
      b.className = 'suggestion';
      b.textContent = item.label;
      b.onclick = () => send(item.message);
      area.append(b);
    });
    wrap.append(area);
  }
  chatBody.append(wrap);
  chatBody.scrollTop = chatBody.scrollHeight;
}

function showChecklist(steps) {
  checklist.className = 'checklist';
  checklist.innerHTML = '';
  if (!steps || !steps.length) {
    checklist.className = 'checklist empty-checklist';
    checklist.innerHTML = '<div class="empty-icon">☷</div><b>No steps yet</b><span>Start a conversation to get guided help.</span>';
    return;
  }
  steps.forEach((s) => {
    const label = document.createElement('label');
    label.className = 'check-item' + (attempted.includes(s) ? ' done' : '');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = attempted.includes(s);
    cb.onchange = () => {
      label.classList.toggle('done', cb.checked);
      if (cb.checked && !attempted.includes(s)) {
        attempted.push(s);
      } else if (!cb.checked && attempted.includes(s)) {
        attempted = attempted.filter(item => item !== s);
      }
    };
    const span = document.createElement('span');
    span.textContent = s;
    label.append(cb, span);
    checklist.append(label);
  });
  const note = document.createElement('p');
  note.className = 'small muted';
  note.textContent = 'Tick a step after trying it. The agent will avoid suggesting it again.';
  checklist.append(note);
}

async function refreshTicketCount() {
  if (!currentUser) {
    // Guests: count by guest_session_id
    try {
      const res = await fetch(`${API_BASE}/tickets?guest_session_id=${encodeURIComponent(guestSessionId)}`);
      if (res.ok) {
        const t = await res.json();
        $('#ticketCount').textContent = t.length;
      } else {
        $('#ticketCount').textContent = '0';
      }
    } catch (e) {
      $('#ticketCount').textContent = '0';
    }
    return;
  }
  try {
    const url = currentUser.role === 'it'
      ? `${API_BASE}/tickets`
      : `${API_BASE}/tickets?employee_id=${encodeURIComponent(currentUser.id)}`;
    const res = await fetch(url, { headers: getAuthHeaders() });
    if (res.ok) {
      tickets = await res.json();
      $('#ticketCount').textContent = tickets.length;
    }
  } catch (e) {
    console.warn('Could not refresh tickets count:', e);
  }
}

async function send(text) {
  text = (text || '').trim();
  if (!text) return;

  // Idempotency guard: block duplicate sends while a request is in-flight
  if (_sendInProgress) return;
  _sendInProgress = true;

  input.value = '';
  addMessage(text, 'user');

  const low = text.toLowerCase();
  if (/password is|my password|otp is|one-time code|auth code|secret/i.test(low)) {
    addMessage('For your safety, please do not share passwords, PINs, or verification codes. Remove those details and describe only the technical error or symptom.');
    _sendInProgress = false;
    return;
  }

  // Display temporary typing indicator
  const typingWrap = document.createElement('div');
  typingWrap.className = 'message agent-message typing-indicator';
  typingWrap.innerHTML = '<div class="message-label">IT Support Agent · typing</div><p class="muted small">Checking records and guide…</p>';
  chatBody.append(typingWrap);
  chatBody.scrollTop = chatBody.scrollHeight;

  try {
    const clientOs = /Macintosh|Mac OS X/i.test(navigator.userAgent) ? 'macOS' : (/Windows/i.test(navigator.userAgent) ? 'Windows' : null);
    const payload = {
      message: text,
      conversation_id: conversationId,
      employee_id: currentUser ? currentUser.id : null,
      requester_name: currentUser ? currentUser.name : null,
      guest_session_id: currentUser ? null : guestSessionId,
      location: currentUser ? currentUser.location : null,
      service: currentService || null,
      os_name: clientOs,
      attempted_steps: attempted
    };

    const res = await fetch(`${API_BASE}/chat/message`, {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify(payload)
    });

    typingWrap.remove();

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      addMessage('Error communicating with advisory server: ' + (err.detail || res.statusText));
      _sendInProgress = false;
      return;
    }

    const data = await res.json();
    currentService = data.service || currentService;

    // Display agent response
    addMessage(data.agent_message, 'agent', data.suggestions);

    // Handle navigation intents
    if (data.next_action === 'open_tickets') {
      await openTickets(data.employee_id || (data.ticket ? data.ticket.employee_id : null));
      _sendInProgress = false;
      return;
    }

    if (data.next_action === 'new_issue') {
      currentIssue = '';
      currentService = '';
      attempted = [];
      showChecklist([]);
      _sendInProgress = false;
      return;
    }

    // Update checklist based on returned steps
    if (data.suggested_steps && data.suggested_steps.length) {
      showChecklist(data.suggested_steps);
    } else if (data.outage_status === 'Active' || data.is_resolved) {
      showChecklist([]);
    }

    // If resolved, reset local session state
    if (data.is_resolved && data.next_action === 'resolved') {
      currentIssue = '';
      currentService = '';
      attempted = [];
      showChecklist([]);
    }

    // Only refresh ticket count and show ticket success when backend confirms ticket creation
    // Do NOT show success on 'clarify' or 'ask_location' next_actions
    if (data.ticket && data.next_action === 'ticket_created') {
      await refreshTicketCount();
    }

  } catch (err) {
    typingWrap.remove();
    console.error('Chat error:', err);
    addMessage('Network connection issue connecting to http://localhost:8000. Please ensure the ResolveIT backend server is running.');
  } finally {
    _sendInProgress = false;
  }
}

function openModal(html) {
  modalContent.innerHTML = html;
  modalBackdrop.hidden = false;
}

function closeModal() {
  modalBackdrop.hidden = true;
}

function openLogin(note = '') {
  openModal(`
    <h2>Sign in to your portal</h2>
    <p>${note || 'Choose your portal to start troubleshooting or manage tickets.'}</p>
    <form class="form-stack" id="loginForm">
      <label>Portal
        <select id="portal">
          <option value="employee">Employee portal</option>
          <option value="it">IT department portal</option>
        </select>
      </label>
      <label>Employee ID
        <input id="employeeId" placeholder="e.g. E1001" required value="${currentUser ? currentUser.id : ''}">
      </label>
      <label>Password
        <input id="password" type="password" placeholder="Enter password" required>
      </label>
      <div id="loginErrBox" class="login-error" hidden></div>
      <button class="btn" type="submit">Sign in</button>
    </form>
    <div class="subtle">Demo: <b>E1001</b> / <b>Employee@123</b> &nbsp;&middot;&nbsp; <b>E1002</b> / <b>Employee@123</b><br>IT Staff: <b>IT1001</b> / <b>Admin@123</b> &rarr; redirects to IT Portal</div>
  `);

  $('#loginForm').onsubmit = async (e) => {
    e.preventDefault();
    const portal = $('#portal').value;
    const employee_id = $('#employeeId').value.trim();
    const password = $('#password').value;
    const errBox = $('#loginErrBox');

    if (errBox) errBox.hidden = true;

    try {
      const res = await fetch(`${API_BASE}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ employee_id, password, portal })
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        if (errBox) {
          errBox.textContent = err.detail || 'Login failed. Invalid credentials or portal mismatch.';
          errBox.hidden = false;
        } else {
          alert(err.detail || 'Login failed. Invalid credentials or portal mismatch.');
        }
        return;
      }

      const prevUserId = currentUser ? currentUser.id : null;
      currentUser = await res.json();

      // IT staff → redirect to dedicated IT portal
      if (currentUser.role && currentUser.role.toLowerCase() === 'it') {
        sessionStorage.setItem('it_portal_user', JSON.stringify(currentUser));
        window.location.href = '/it_department/';
        return;
      }

      // Update header badge
      $('#userBadge').textContent = `${currentUser.name} · ${currentUser.location}`;
      $('#loginBtn').innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg> ${currentUser.name.split(' ')[0]}`;
      closeModal();

      // Always start a fresh conversation on login (new user or switch user)
      try {
        await fetch(`${API_BASE}/chat/reset?conversation_id=${encodeURIComponent(conversationId)}`, { method: 'POST' });
      } catch (_) {}

      conversationId = 'cid-' + Date.now() + '-' + Math.random().toString(36).substring(2, 8);
      localStorage.setItem('resolveit_cid', conversationId);
      chatBody.innerHTML = '';
      currentIssue = '';
      currentService = '';
      attempted = [];
      showChecklist([]);

      // Show fresh welcome without any login notification
      addMessage(
        `Hello, ${currentUser.name.split(' ')[0]}! Tell me what's not working — Wi-Fi, email, printer, or VPN. I'll check for a known outage first.`,
        'agent',
        [
          { label: 'Wi-Fi issue', message: 'My Wi-Fi is not working' },
          { label: 'Printer issue', message: 'My printer is offline' },
          { label: 'Email issue', message: 'My email is not working' },
          { label: 'VPN issue', message: 'My VPN is not connecting' }
        ]
      );

      await refreshTicketCount();
    } catch (err) {
      if (errBox) {
        errBox.textContent = 'Could not connect to backend server: ' + err.message;
        errBox.hidden = false;
      } else {
        alert('Could not connect to backend authentication server: ' + err.message);
      }
    }
  };
}

async function openTickets(empIdOverride = null) {
  // For guests: use guest_session_id to scope My Tickets
  const isGuest = !currentUser;
  const targetEmpId = (currentUser && currentUser.id) || empIdOverride;

  if (isGuest) {
    // Guest view scoped to this session's tickets
    let guestTickets = [];
    try {
      const res = await fetch(`${API_BASE}/tickets?guest_session_id=${encodeURIComponent(guestSessionId)}`);
      guestTickets = res.ok ? await res.json() : [];
    } catch (e) {
      guestTickets = [];
    }
    const content = guestTickets.length ? guestTickets.map(t => `
      <div class="ticket-row">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px;">
          <b style="color:#388bfd; font-size:12px; font-variant-numeric:tabular-nums;">${escapeHtml(t.ticket_id)}</b>
          <span class="status ${t.status.toLowerCase().replace(' ', '-')}">${t.status}</span>
        </div>
        <b style="font-size:13px; color:#e6edf3; display:block; margin:4px 0 3px;">${escapeHtml(t.issue)}</b>
        <small>
          ${escapeHtml(t.service || '')} &middot; ${escapeHtml(t.location || '')} &middot; ${escapeHtml(t.department || '')} &middot; Priority: <b style="color:#c9d1d9;">${t.priority}</b>
        </small>
        ${t.requester_name ? `<div class="resolution-note">Reported by: ${escapeHtml(t.requester_name)}</div>` : ''}
        ${t.resolution_notes ? `<div class="resolution-note">&#10003; ${escapeHtml(t.resolution_notes)}</div>` : ''}
      </div>
    `).join('') : '<p class="muted" style="padding:16px 0;">You have no recorded support tickets in this session.</p>';
    openModal(`
      <h2>My Tickets (Guest Session)</h2>
      <p>Tickets raised in your current browser session.</p>
      <div style="max-height: 52vh; overflow-y: auto; margin-top:14px; scrollbar-width:thin;">${content}</div>
      <div style="display:flex; justify-content:space-between; margin-top:16px; gap:8px;">
        <button class="btn btn-soft" id="refreshTicketsBtn">&#8635; Refresh</button>
        <button class="btn btn-outline" onclick="closeModal()">Close</button>
      </div>
    `);
    $('#refreshTicketsBtn').onclick = () => openTickets();
    return;
  }

  const role = currentUser.role;
  if (role === 'employee') {
    // Employee ticket view
    try {
      const res = await fetch(`${API_BASE}/tickets?employee_id=${encodeURIComponent(targetEmpId)}`, { headers: getAuthHeaders() });
      tickets = res.ok ? await res.json() : [];
    } catch (e) {
      tickets = [];
    }

    const content = tickets.length ? tickets.map(t => `
      <div class="ticket-row">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px;">
          <b style="color:#388bfd; font-size:12px; font-variant-numeric:tabular-nums;">${escapeHtml(t.ticket_id)}</b>
          <span class="status ${t.status.toLowerCase().replace(' ', '-')}">${t.status}</span>
        </div>
        <b style="font-size:13px; color:#e6edf3; display:block; margin:4px 0 3px;">${escapeHtml(t.issue)}</b>
        <small>
          ${escapeHtml(t.service || '')} &middot; ${escapeHtml(t.location || '')} &middot; ${escapeHtml(t.department || '')} &middot; Priority: <b style="color:#c9d1d9;">${t.priority}</b>
        </small>
        ${t.resolution_notes ? `<div class="resolution-note">&#10003; ${escapeHtml(t.resolution_notes)}</div>` : ''}
      </div>
    `).join('') : '<p class="muted" style="padding:16px 0;">You have no recorded support tickets yet.</p>';

    openModal(`
      <h2>My Tickets</h2>
      <p>Track your submitted IT incident requests.</p>
      <div style="max-height: 52vh; overflow-y: auto; margin-top:14px; scrollbar-width:thin;">${content}</div>
      <div style="display:flex; justify-content:space-between; margin-top:16px; gap:8px;">
        <button class="btn btn-soft" id="refreshTicketsBtn">&#8635; Refresh</button>
        <button class="btn btn-outline" onclick="closeModal()">Close</button>
      </div>
    `);
    $('#refreshTicketsBtn').onclick = () => openTickets(empIdOverride);

  } else {
    // IT Department Incident Management Dashboard
    let metrics = { total_tickets: 0, open_tickets: 0, in_progress_tickets: 0, resolved_tickets: 0, active_outages: 0 };
    try {
      const mRes = await fetch(`${API_BASE}/dashboard/metrics`, { headers: getAuthHeaders() });
      if (mRes.ok) metrics = await mRes.json();
      const tRes = await fetch(`${API_BASE}/tickets`, { headers: getAuthHeaders() });
      if (tRes.ok) tickets = await tRes.json();
    } catch (e) {
      console.warn('Dashboard fetch error:', e);
    }

    renderITDashboard(metrics, tickets);
  }
}

function renderITDashboard(metrics, ticketsList) {
  const metricsHtml = `
    <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 14px 0 18px;">
      <div style="background:#f4f7fb; padding:10px; border-radius:8px; text-align:center;">
        <div style="font-size:18px; font-weight:700; color:var(--blue);">${metrics.total_tickets}</div>
        <small style="color:var(--muted); font-size:11px;">Total</small>
      </div>
      <div style="background:#fff6e6; padding:10px; border-radius:8px; text-align:center;">
        <div style="font-size:18px; font-weight:700; color:#d97706;">${metrics.open_tickets}</div>
        <small style="color:var(--muted); font-size:11px;">Open</small>
      </div>
      <div style="background:#eef4fc; padding:10px; border-radius:8px; text-align:center;">
        <div style="font-size:18px; font-weight:700; color:var(--blue);">${metrics.in_progress_tickets}</div>
        <small style="color:var(--muted); font-size:11px;">In Progress</small>
      </div>
      <div style="background:#e6f8ef; padding:10px; border-radius:8px; text-align:center;">
        <div style="font-size:18px; font-weight:700; color:var(--green);">${metrics.resolved_tickets}</div>
        <small style="color:var(--muted); font-size:11px;">Resolved</small>
      </div>
    </div>
  `;

  const rows = ticketsList.map(t => `
    <div class="ticket-row" style="border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin-bottom: 10px; background: #fff;">
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <b>${t.ticket_id}</b> · <span class="status ${t.status.toLowerCase().replace(' ', '-')}">${t.status}</span>
          <div style="font-size:13px; font-weight:600; margin-top:2px;">${escapeHtml(t.issue)}</div>
        </div>
        <span style="font-size:12px; font-weight:700; color:${t.priority==='Critical'?'#dc2626':t.priority==='High'?'#ea580c':'#4b5563'}">${t.priority}</span>
      </div>
      <small style="color:var(--muted); display:block; margin: 4px 0 8px;">
        Emp: <b>${t.employee_id}</b> (${t.location}) · ${t.service} · Tech: <b>${t.assigned_technician || 'Unassigned'}</b>
      </small>
      <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:6px;">
        <button class="btn btn-soft small" onclick="openTicketActionModal('${t.ticket_id}')">⚙ Manage Ticket</button>
      </div>
    </div>
  `).join('') || '<p class="muted">No support tickets match the current criteria.</p>';

  openModal(`
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <h2>IT Incident Management Portal</h2>
    </div>
    <p class="muted small">Authenticated as IT Administrator (${currentUser.id}) · Live SQLite database</p>
    ${metricsHtml}
    <div style="display:flex; gap:8px; margin-bottom:12px;">
      <input id="itSearch" placeholder="Search ticket ID or issue…" style="flex:1; border:1px solid var(--line); border-radius:8px; padding:8px 10px; font-size:13px;">
      <button class="btn btn-outline small" id="reindexBtn" title="Re-index approved IT guides">↻ Re-index Guides</button>
    </div>
    <div style="max-height: 48vh; overflow-y: auto; padding-right: 4px;">${rows}</div>
    <div style="display:flex; justify-content:flex-end; margin-top:14px;">
      <button class="btn btn-outline" onclick="closeModal()">Close</button>
    </div>
  `);

  $('#itSearch').oninput = (e) => {
    const q = e.target.value.toLowerCase();
    const filtered = tickets.filter(t => 
      t.ticket_id.toLowerCase().includes(q) || 
      t.issue.toLowerCase().includes(q) || 
      t.employee_id.toLowerCase().includes(q)
    );
    renderITDashboard(metrics, filtered);
  };

  $('#reindexBtn').onclick = async () => {
    try {
      const res = await fetch(`${API_BASE}/guides/reindex`, {
        method: 'POST',
        headers: getAuthHeaders()
      });
      if (res.ok) {
        const d = await res.json();
        alert(`Guide index successfully refreshed: ${d.indexed_chunks} chunks indexed from ${d.source_files.join(', ')}.`);
      } else {
        alert('Guide re-indexing failed.');
      }
    } catch (err) {
      alert('Error connecting to guide service: ' + err.message);
    }
  };
}

window.openTicketActionModal = async function(ticketId) {
  try {
    const res = await fetch(`${API_BASE}/tickets/${ticketId}`, { headers: getAuthHeaders() });
    if (!res.ok) return alert('Failed to load ticket details.');
    const t = await res.json();

    const historyHtml = (t.history || []).map(h => `
      <li style="font-size:12px; margin-bottom:6px; color:var(--muted);">
        <b>${h.timestamp}</b> [${h.performed_by}]: ${escapeHtml(h.details)}
      </li>
    `).join('') || '<li class="muted small">No history recorded yet.</li>';

    openModal(`
      <h2>Manage Ticket ${t.ticket_id}</h2>
      <p style="font-size:13px;"><b>Issue:</b> ${escapeHtml(t.issue)}</p>
      <form class="form-stack" id="ticketUpdateForm">
        <label>Status
          <select id="updateStatus">
            <option value="Open" ${t.status==='Open'?'selected':''}>Open</option>
            <option value="In progress" ${t.status==='In progress'||t.status==='In Progress'?'selected':''}>In Progress</option>
            <option value="Resolved" ${t.status==='Resolved'?'selected':''}>Resolved</option>
            <option value="Closed" ${t.status==='Closed'?'selected':''}>Closed</option>
          </select>
        </label>
        <label>Priority
          <select id="updatePriority">
            <option value="Low" ${t.priority==='Low'?'selected':''}>Low</option>
            <option value="Medium" ${t.priority==='Medium'?'selected':''}>Medium</option>
            <option value="High" ${t.priority==='High'?'selected':''}>High</option>
            <option value="Critical" ${t.priority==='Critical'?'selected':''}>Critical</option>
          </select>
        </label>
        <label>Assigned Technician
          <input id="updateTech" value="${t.assigned_technician || ''}" placeholder="e.g. IT1001, Tech-Priya">
        </label>
        <label>Resolution Notes
          <textarea id="updateResolution" rows="2" style="border:1px solid var(--line); border-radius:8px; padding:8px; font:inherit;">${t.resolution_notes || ''}</textarea>
        </label>
        <div style="display:flex; gap:8px; margin-top:10px;">
          <button class="btn" type="submit">Save Changes</button>
          <button class="btn btn-outline" type="button" onclick="openTickets()">Back to Dashboard</button>
        </div>
      </form>
      <div style="margin-top:16px; border-top:1px solid var(--line); padding-top:12px;">
        <b style="font-size:13px;">Audit & History Trail:</b>
        <ul style="padding-left:18px; margin-top:8px;">${historyHtml}</ul>
      </div>
    `);

    $('#ticketUpdateForm').onsubmit = async (e) => {
      e.preventDefault();
      const status = $('#updateStatus').value;
      const priority = $('#updatePriority').value;
      const assigned_technician = $('#updateTech').value.trim();
      const resolution_notes = $('#updateResolution').value.trim();

      const patchRes = await fetch(`${API_BASE}/tickets/${t.ticket_id}`, {
        method: 'PATCH',
        headers: getAuthHeaders(),
        body: JSON.stringify({ status, priority, assigned_technician, resolution_notes })
      });

      if (patchRes.ok) {
        alert(`Ticket ${t.ticket_id} updated successfully.`);
        await openTickets();
      } else {
        const err = await patchRes.json().catch(() => ({}));
        alert(err.detail || 'Ticket update failed.');
      }
    };
  } catch (err) {
    alert('Error loading ticket: ' + err.message);
  }
};

function escapeHtml(s) {
  if (!s) return '';
  return s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Event Listeners
$('#chatForm').addEventListener('submit', e => {
  e.preventDefault();
  send(input.value);
});

document.querySelectorAll('[data-question]').forEach(b => {
  b.addEventListener('click', () => send(b.dataset.question));
});

$('#loginBtn').onclick = () => openLogin();
$('#ticketsBtn').onclick = openTickets;
$('#closeModal').onclick = closeModal;
modalBackdrop.addEventListener('click', e => {
  if (e.target === modalBackdrop) closeModal();
});

$('#clearBtn').onclick = async () => {
  try {
    await fetch(`${API_BASE}/chat/reset?conversation_id=${encodeURIComponent(conversationId)}`, { method: 'POST' });
  } catch (e) {}
  conversationId = 'cid-' + Date.now() + '-' + Math.random().toString(36).substring(2, 8);
  localStorage.setItem('resolveit_cid', conversationId);
  chatBody.innerHTML = '';
  currentIssue = '';
  currentService = '';
  attempted = [];
  showChecklist([]);
  addMessage('Conversation cleared. Tell me what is not working and I’ll check outage records first.', 'agent', [
    { label: 'Wi-Fi issue', message: 'My Wi-Fi is not working' },
    { label: 'Printer issue', message: 'My printer is offline' },
    { label: 'Email issue', message: 'My email is not working' }
  ]);
};

// Initial setup
refreshTicketCount();
