// State
let sessionType = null; // 'in' | 'out' | null
let rows = {};          // barcode → {session_delta, name, brand, weight, price}
let endStripOpen = false;

// WebSocket
let ws;
const RECONNECT_DELAY = 2000;

function connectWS() {
    ws = new WebSocket(`ws://${window.location.host}/ws`);
    ws.onopen = () => {};
    ws.onmessage = (event) => {
        handleWSMessage(JSON.parse(event.data));
    };
    ws.onclose = () => {
        setTimeout(connectWS, RECONNECT_DELAY);
    };
}

// ── HTML helpers ────────────────────────────────────────────────────────────

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function renderField(field) {
    if (field.status === 'loading') return '<span class="italic-muted">loading…</span>';
    if (field.status === 'failed')  return '<span class="muted">failed</span>';
    if (field.value === null)       return '<span class="muted">unknown</span>';
    return esc(String(field.value));
}

function renderPrice(field) {
    if (field.status === 'loading') return '<span class="italic-muted">loading…</span>';
    if (field.status === 'failed')  return '<span class="muted">failed</span>';
    if (field.value === null)       return '<span class="muted">unknown</span>';
    return '£' + field.value.toFixed(2);
}

// ── Banner ──────────────────────────────────────────────────────────────────

function totalDelta() {
    return Object.values(rows).reduce((sum, r) => sum + r.session_delta, 0);
}

function updateBanner() {
    const banner  = document.getElementById('banner');
    const text    = document.getElementById('banner-text');
    const endBtn  = document.getElementById('btn-end-session');
    const total   = totalDelta();

    if (sessionType === null) {
        banner.className = 'banner-idle';
        text.textContent = 'No active session';
        endBtn.style.display = 'none';
    } else if (sessionType === 'in') {
        banner.className = 'banner-in';
        text.textContent = `Scan in — ${total} units`;
        endBtn.style.display = '';
    } else {
        banner.className = 'banner-out';
        text.textContent = `Scan out — ${total} units`;
        endBtn.style.display = '';
    }
}

// ── Feed rows ────────────────────────────────────────────────────────────────

function makeRowHTML(barcode) {
    const r = rows[barcode];
    const deltaClass = sessionType === 'out' ? 'feed-delta-out' : 'feed-delta-in';
    const deltaSign  = sessionType === 'out' ? '−' : '+';
    const meta = [renderField(r.brand), renderField(r.weight), renderPrice(r.price)].join(' · ');

    // TODO Phase 3: wire quantity controls to PUT /session/items/{barcode}
    return `
      <div class="feed-row-top">
        <span class="feed-item-name">${renderField(r.name)}</span>
        <span class="${deltaClass}">${deltaSign}${r.session_delta}</span>
      </div>
      <div class="feed-item-meta">${meta}</div>
      <div class="feed-item-qty">
        <button disabled aria-label="Decrease">−</button>
        <span class="qty-count">${r.session_delta}</span>
        <button disabled aria-label="Increase">+</button>
      </div>`;
}

function updateRowDOM(barcode) {
    const el = document.getElementById('row-' + barcode);
    if (el) el.innerHTML = makeRowHTML(barcode);
}

function prependRowToFeed(barcode) {
    const list = document.getElementById('feed-list');
    const li = document.createElement('li');
    li.id = 'row-' + barcode;
    li.className = 'feed-row';
    li.innerHTML = makeRowHTML(barcode);
    list.prepend(li);
}

function appendRowToFeed(barcode) {
    const list = document.getElementById('feed-list');
    const li = document.createElement('li');
    li.id = 'row-' + barcode;
    li.className = 'feed-row';
    li.innerHTML = makeRowHTML(barcode);
    list.append(li);
}

// ── WebSocket message handler ────────────────────────────────────────────────

function handleWSMessage(msg) {
    if (msg.type === 'scan') {
        rows[msg.barcode] = {
            session_delta: msg.session_delta,
            name: msg.name, brand: msg.brand,
            weight: msg.weight, price: msg.price,
        };
        if (document.getElementById('row-' + msg.barcode)) {
            updateRowDOM(msg.barcode);
        } else {
            prependRowToFeed(msg.barcode);
        }
    } else if (msg.type === 'resolution') {
        if (rows[msg.barcode] !== undefined) {
            rows[msg.barcode] = {
                session_delta: msg.session_delta,
                name: msg.name, brand: msg.brand,
                weight: msg.weight, price: msg.price,
            };
            updateRowDOM(msg.barcode);
        }
    }
    updateBanner();
    if (endStripOpen) {
        updateStripSummary();
        evaluateConfirmGate();
    }
}

// ── End-session strip ────────────────────────────────────────────────────────

function evaluateConfirmGate() {
    const hasPending = Object.values(rows).some(r =>
        r.name.status === 'loading' || r.price.status === 'loading'
    );
    const btn = document.getElementById('btn-confirm');
    btn.disabled = hasPending;
    btn.textContent = hasPending ? 'loading…' : 'Confirm';
}

function updateStripSummary() {
    const total  = totalDelta();
    const action = sessionType === 'in' ? 'Add' : 'Remove';
    document.getElementById('strip-summary').textContent =
        `Confirm: ${action} ${total} units to inventory?`;
}

function showEndStrip() {
    endStripOpen = true;
    document.getElementById('end-strip').style.display = '';
    document.getElementById('dim-overlay').style.display = '';
    document.getElementById('strip-error').style.display = 'none';
    updateStripSummary();
    evaluateConfirmGate();
}

function hideEndStrip() {
    endStripOpen = false;
    document.getElementById('end-strip').style.display = 'none';
    document.getElementById('dim-overlay').style.display = 'none';
}

// ── Discard modal ────────────────────────────────────────────────────────────

function showDiscardModal() {
    document.getElementById('discard-modal').classList.add('visible');
}

function hideDiscardModal() {
    document.getElementById('discard-modal').classList.remove('visible');
}

// ── State transitions ────────────────────────────────────────────────────────

function resetToNoSession() {
    sessionType = null;
    rows = {};
    document.getElementById('feed-list').innerHTML = '';
    document.getElementById('no-session').style.display = '';
    document.getElementById('active-session').style.display = 'none';
    hideEndStrip();
    hideDiscardModal();
    document.getElementById('negative-modal').classList.remove('visible');
    updateBanner();
}

function renderActiveSession(session) {
    sessionType = session.type;
    rows = {};
    document.getElementById('no-session').style.display = 'none';
    document.getElementById('active-session').style.display = '';
    document.getElementById('feed-list').innerHTML = '';
    for (const item of session.items) {
        rows[item.barcode] = {
            session_delta: item.delta, // GET /session uses 'delta'; WS uses 'session_delta'
            name: item.name, brand: item.brand,
            weight: item.weight, price: item.price,
        };
        appendRowToFeed(item.barcode);
    }
    updateBanner();
}

// ── API actions ──────────────────────────────────────────────────────────────

async function startSession(type) {
    try {
        const resp = await fetch('/session', {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' },
            body: JSON.stringify({ type }),
        });
        if (resp.status === 201) {
            const data = await resp.json();
            renderActiveSession(data.session);
        }
    } catch (_) {}
}

async function confirmSession() {
    const btn = document.getElementById('btn-confirm');
    btn.disabled = true;
    document.getElementById('strip-error').style.display = 'none';
    try {
        const resp = await fetch('/session/confirm', {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY },
        });
        if (resp.ok) {
            resetToNoSession();
        } else if (resp.status === 409) {
            const data = await resp.json();
            const err = data.detail?.error;
            if (err === 'lookups_pending') {
                const errEl = document.getElementById('strip-error');
                errEl.textContent = 'Some lookups are still pending. Please wait.';
                errEl.style.display = '';
                evaluateConfirmGate();
            } else if (err === 'would_go_negative') {
                hideEndStrip();
                showNegativeModal(data.detail?.items ?? []);
            }
        }
    } catch (_) {
        evaluateConfirmGate();
    }
}

async function discardSession() {
    try {
        const resp = await fetch('/session/discard', {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY },
        });
        if (resp.ok) {
            resetToNoSession();
        }
    } catch (_) {
        hideDiscardModal();
    }
}

function showNegativeModal(items) {
    const el = document.getElementById('negative-items');
    if (!items.length) {
        el.textContent = 'Some items would go negative. Adjust quantities before confirming.';
    } else {
        el.innerHTML = items.map(item =>
            `<div class="negative-item">${esc(item.barcode)}: stock ${item.current_quantity}, removing ${item.delta}</div>`
        ).join('');
    }
    document.getElementById('negative-modal').classList.add('visible');
}

// ── Toast ────────────────────────────────────────────────────────────────────

function showToast(msg) {
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.classList.add('visible');
    const t = setTimeout(() => toast.classList.remove('visible'), 4000);
    toast.onclick = () => { clearTimeout(t); toast.classList.remove('visible'); };
}

// ── Init ─────────────────────────────────────────────────────────────────────

async function init() {
    document.getElementById('btn-start-in').addEventListener('click', () => startSession('in'));
    document.getElementById('btn-start-out').addEventListener('click', () => startSession('out'));
    document.getElementById('btn-end-session').addEventListener('click', showEndStrip);
    document.getElementById('btn-confirm').addEventListener('click', confirmSession);
    document.getElementById('btn-discard').addEventListener('click', showDiscardModal);
    document.getElementById('btn-yes-discard').addEventListener('click', discardSession);
    document.getElementById('btn-keep').addEventListener('click', hideDiscardModal);
    document.getElementById('btn-close-negative').addEventListener('click', () => {
        document.getElementById('negative-modal').classList.remove('visible');
    });

    try {
        const resp = await fetch('/session', { headers: { 'X-API-Key': API_KEY } });
        if (resp.ok) {
            const data = await resp.json();
            if (data.session) {
                renderActiveSession(data.session);
                if (data.session.recovered_at) {
                    const diff = Date.now() - new Date(data.session.recovered_at).getTime();
                    if (diff < 5 * 60 * 1000) {
                        showToast('Session resumed after restart');
                    }
                }
            }
        }
    } catch (_) {}

    connectWS();
}

document.addEventListener('DOMContentLoaded', init);
