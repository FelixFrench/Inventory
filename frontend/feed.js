// State
let sessionType = null; // 'in' | 'out' | null
let rows = {};          // rowKey(barcode, retailer) → {barcode, retailer, session_delta, name, brand, quantity, price}
let endStripOpen = false;

// WebSocket
let ws;
const RECONNECT_DELAY = 2000;

function connectWS() {
    ws = new WebSocket(`ws://${window.location.host}/ws`);
    ws.onmessage = (event) => {
        handleWSMessage(JSON.parse(event.data));
    };
    ws.onclose = () => {
        setTimeout(connectWS, RECONNECT_DELAY);
    };
}

// ── HTML helpers ────────────────────────────────────────────────────────────
// esc() lives in shared-utils.js (loaded before this script on every page).

// Returns the shared loading/failed markup, or null if the field has a value to display.
function _statusMarkup(field) {
    if (field.status === 'loading') return '<span class="italic-muted">loading…</span>';
    if (field.status === 'failed')  return '<span class="muted">failed</span>';
    return null;
}

function renderField(field) {
    return _statusMarkup(field) ?? (field.value === null ? '<span class="muted">unknown</span>' : esc(String(field.value)));
}

function renderPrice(field) {
    return _statusMarkup(field) ?? (field.value === null ? '<span class="muted">unknown</span>' : '£' + field.value.toFixed(2));
}

// ── Banner ──────────────────────────────────────────────────────────────────

function totalDelta() {
    return Object.values(rows).reduce((sum, r) => sum + r.session_delta, 0);
}

function updateBanner() {
    const banner = document.getElementById('banner');
    const text   = document.getElementById('banner-text');
    const endBtn = document.getElementById('btn-end-session');
    const total  = totalDelta();

    banner.classList.toggle('banner-idle', sessionType === null);
    banner.classList.toggle('banner-in',   sessionType === 'in');
    banner.classList.toggle('banner-out',  sessionType === 'out');

    if (sessionType === null) {
        text.textContent = 'No active session';
        endBtn.classList.add('hidden');
    } else if (sessionType === 'in') {
        text.textContent = `Scan in — ${total} units`;
        endBtn.classList.remove('hidden');
    } else {
        text.textContent = `Scan out — ${total} units`;
        endBtn.classList.remove('hidden');
    }
}

// ── Feed rows ────────────────────────────────────────────────────────────────

function makeRowHTML(key) {
    const r = rows[key];
    const deltaClass = sessionType === 'out' ? 'feed-delta-out' : 'feed-delta-in';
    const deltaSign  = sessionType === 'out' ? '−' : '+';

    const decDisabled = r.session_delta === 0 ? ' disabled' : '';
    // Product name links to the internal product-info page (server-supplied).
    const pageHref = sanitiseHref(r.product_page_url);
    const nameLink = pageHref
        ? `<a href="${esc(pageHref)}" class="feed-item-name">${renderField(r.name)}</a>`
        : `<span class="feed-item-name">${renderField(r.name)}</span>`;
    const priceHref = sanitiseHref(r.price_url);
    const priceHtml = priceHref
        ? `<a href="${esc(priceHref)}" target="_blank" rel="noopener noreferrer">${renderPrice(r.price)}</a>`
        : renderPrice(r.price);
    const meta = [renderField(r.brand), renderField(r.quantity), priceHtml].join(' · ');
    return `
      <div class="feed-row-top">
        ${nameLink}
        <div class="feed-row-right">
          <span class="${deltaClass}">${deltaSign}${r.session_delta}</span>
          <span class="feed-item-stock">${r.inventory_quantity ?? 0} in stock</span>
        </div>
      </div>
      <div class="feed-item-meta">${meta}</div>
      <div class="feed-item-qty">
        <button class="qty-btn" data-action="decrement" data-barcode="${esc(r.barcode)}" data-retailer="${r.retailer}"${decDisabled} aria-label="Decrease">−</button>
        <span class="qty-count" data-barcode="${esc(r.barcode)}" data-retailer="${r.retailer}">${r.session_delta}</span>
        <button class="qty-btn" data-action="increment" data-barcode="${esc(r.barcode)}" data-retailer="${r.retailer}" aria-label="Increase">+</button>
      </div>`;
}

function updateRowDOM(key) {
    // Row id carries the composite key (row-<barcode>:<retailer>). The colon is only ever
    // consumed via getElementById (a plain string match, never a CSS/querySelector token).
    const el = document.getElementById('row-' + key);
    if (!el) return;
    el.innerHTML = makeRowHTML(key);
    const overStock = sessionType === 'out' &&
                      rows[key].session_delta > rows[key].inventory_quantity;
    el.classList.toggle('feed-row--warning', overStock);
}

function addRowToFeed(key, prepend = false) {
    const list = document.getElementById('feed-list');
    const li = document.createElement('li');
    li.id = 'row-' + key;
    li.className = 'feed-row';
    li.innerHTML = makeRowHTML(key);
    const overStock = sessionType === 'out' &&
                      rows[key].session_delta > rows[key].inventory_quantity;
    if (overStock) li.classList.add('feed-row--warning');
    prepend ? list.prepend(li) : list.append(li);
}

// ── Quantity controls ────────────────────────────────────────────────────────

async function handleDeltaChange(key, diff) {
    const r = rows[key];
    if (!r) return;
    const newDelta = Math.max(0, (r.session_delta ?? 0) + diff);
    try {
        const resp = await fetch(overrideUrl(r), {
            method: 'PUT',
            headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' },
            body: JSON.stringify({ delta: newDelta }),
        });
        if (resp.ok) {
            rows[key].session_delta = newDelta;
            updateRowDOM(key);
            updateBanner();
        } else {
            showToast('Could not update quantity');
        }
    } catch (_) {
        showToast('Could not update quantity — check connection');
    }
}

// Quantity-override endpoint for a row: PUT /session/items/{barcode}/{retailer_id}.
function overrideUrl(r) {
    return `/session/items/${encodeURIComponent(r.barcode)}/${encodeURIComponent(r.retailer)}`;
}

function activateInlineEdit(span) {
    const barcode = span.dataset.barcode;
    const retailer = span.dataset.retailer;
    const key = rowKey(barcode, retailer);
    const current = rows[key]?.session_delta ?? 0;
    const input = document.createElement('input');
    input.type = 'number';
    input.min = '0';
    input.inputMode = 'numeric';
    input.value = current;
    input.className = 'qty-inline-input';
    span.replaceWith(input);
    input.select();

    async function commit() {
        const val = Math.max(0, parseInt(input.value) || 0);
        const li = input.closest('li');
        if (li) li.dataset.editing = '';
        // Restore span before the async call so the row is usable immediately
        const newSpan = document.createElement('span');
        newSpan.className = 'qty-count';
        newSpan.dataset.barcode = barcode;
        newSpan.dataset.retailer = retailer;
        newSpan.textContent = val;
        input.replaceWith(newSpan);
        if (val !== current) {
            // Optimistic update — revert to `current` if the PUT does not persist.
            rows[key].session_delta = val;
            updateRowDOM(key);
            updateBanner();
            const revert = () => {
                rows[key].session_delta = current;
                updateRowDOM(key);
                updateBanner();
            };
            try {
                const resp = await fetch(overrideUrl(rows[key]), {
                    method: 'PUT',
                    headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' },
                    body: JSON.stringify({ delta: val }),
                });
                if (!resp.ok) {
                    revert();
                    showToast('Could not update quantity');
                }
            } catch (_) {
                revert();
                showToast('Could not update quantity — check connection');
            }
        }
    }

    function cancel() {
        const li = input.closest('li');
        if (li) li.dataset.editing = '';
        const newSpan = document.createElement('span');
        newSpan.className = 'qty-count';
        newSpan.dataset.barcode = barcode;
        newSpan.dataset.retailer = retailer;
        newSpan.textContent = current;
        input.replaceWith(newSpan);
    }

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
    input.addEventListener('blur', commit);
    const li = input.closest('li');
    if (li) li.dataset.editing = 'true';
}

// ── WebSocket message handler ────────────────────────────────────────────────

// Replaces rows[key] wholesale. Stores barcode/retailer (so DOM/URL builders can recover
// the full tuple) plus the fields present on BOTH scan and resolution messages — those may
// legitimately arrive null at scan time and non-null on resolution, so they must overwrite.
// inventory_quantity is deliberately NOT stored here: it is scan-only, so the resolution
// handler saves and restores it around this call.
function _storeRow(key, msg) {
    rows[key] = {
        barcode: msg.barcode,
        retailer: msg.retailer,
        session_delta: msg.session_delta,
        name: msg.name, brand: msg.brand,
        quantity: msg.quantity, price: msg.price,
        off_url: msg.off_url,
        product_page_url: msg.product_page_url,
        price_url: msg.price_url,
    };
}

function handleWSMessage(msg) {
    if (msg.type === 'scan') {
        // Sessionless scans (in_session === false) are broadcast to the search page,
        // not the feed — ignore them here so no phantom row is injected.
        if (msg.in_session === false) return;
        const key = rowKey(msg.barcode, msg.retailer);
        _storeRow(key, msg);
        rows[key].inventory_quantity = msg.inventory_quantity ?? 0;
        if (document.getElementById('row-' + key)) {
            updateRowDOM(key);
        } else {
            addRowToFeed(key, true);
        }
    } else if (msg.type === 'resolution') {
        const key = rowKey(msg.barcode, msg.retailer);
        if (rows[key] !== undefined) {
            const savedQty = rows[key].inventory_quantity ?? 0;
            _storeRow(key, msg);
            rows[key].inventory_quantity = savedQty;
            updateRowDOM(key);
        }
    } else if (msg.type === 'delta_update') {
        const key = rowKey(msg.barcode, msg.retailer);
        if (rows[key] !== undefined) {
            rows[key].session_delta = msg.session_delta;
            updateRowDOM(key);
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
    const prep   = sessionType === 'in' ? 'to'  : 'from';
    document.getElementById('strip-summary').textContent =
        `Confirm: ${action} ${total} units ${prep} inventory?`;
}

function showEndStrip() {
    endStripOpen = true;
    document.getElementById('end-strip').classList.add('visible');
    document.getElementById('dim-overlay').classList.add('visible');
    document.getElementById('strip-error').classList.remove('visible');
    updateStripSummary();
    evaluateConfirmGate();
}

function hideEndStrip() {
    endStripOpen = false;
    document.getElementById('end-strip').classList.remove('visible');
    document.getElementById('dim-overlay').classList.remove('visible');
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
    document.getElementById('no-session').classList.remove('hidden');
    document.getElementById('active-session').classList.add('hidden');
    hideEndStrip();
    hideDiscardModal();
    document.getElementById('negative-modal').classList.remove('visible');
    updateBanner();
}

function renderActiveSession(session) {
    sessionType = session.type;
    rows = {};
    document.getElementById('no-session').classList.add('hidden');
    document.getElementById('active-session').classList.remove('hidden');
    document.getElementById('feed-list').innerHTML = '';
    for (const item of session.items) {
        const key = rowKey(item.barcode, item.retailer);
        rows[key] = {
            barcode: item.barcode,
            retailer: item.retailer,
            session_delta: item.delta, // GET /session uses 'delta'; WS uses 'session_delta'
            inventory_quantity: item.inventory_quantity ?? 0,
            name: item.name, brand: item.brand,
            quantity: item.quantity, price: item.price,
            off_url: item.off_url,
            product_page_url: item.product_page_url,
            price_url: item.price_url,
        };
        addRowToFeed(key);
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
        } else if (resp.status === 409) {
            showToast('A session is already active');
        } else {
            showToast('Could not start session');
        }
    } catch (_) {
        showToast('Could not start session — check connection');
    }
}

async function confirmSession() {
    const btn = document.getElementById('btn-confirm');
    btn.disabled = true;
    document.getElementById('strip-error').classList.remove('visible');
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
                errEl.classList.add('visible');
                evaluateConfirmGate();
            } else if (err === 'would_go_negative') {
                hideEndStrip();
                showNegativeModal();
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

function showNegativeModal() {
    document.getElementById('negative-items').textContent =
        'One or more items are highlighted. Adjust quantities and try again.';
    document.getElementById('negative-modal').classList.add('visible');
}

// showToast() lives in shared-utils.js (loaded before this script).

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

    document.getElementById('feed-list').addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action]');
        if (btn && !btn.disabled) {
            const key = rowKey(btn.dataset.barcode, btn.dataset.retailer);
            const li = btn.closest('li');
            if (li && li.dataset.editing === 'true') return;
            if (btn.dataset.action === 'decrement') handleDeltaChange(key, -1);
            if (btn.dataset.action === 'increment') handleDeltaChange(key, +1);
            return;
        }
        const span = e.target.closest('.qty-count[data-barcode]');
        if (span) {
            const li = span.closest('li');
            if (li && li.dataset.editing === 'true') return;
            activateInlineEdit(span);
        }
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
