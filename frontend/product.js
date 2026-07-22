// Product-info page. Addressed /product.html?barcode=...&retailer_id=...
// esc(), sanitiseHref(), showToast() live in shared-utils.js (loaded first).

const params = new URLSearchParams(window.location.search);
const BARCODE = params.get('barcode') || '';
// Fall back to retailer 1 — the seeded Sainsbury's retailer_id (see initial_schema.sql).
// Server-generated links always include retailer_id; this only covers a hand-typed/bookmarked URL.
const RETAILER_ID = params.get('retailer_id') || '1';

let currentGroups = [];   // memberships on this variant: [{id, name, group_page_url}]
let allGroups = [];       // every group from GET /groups, for the add dropdown

// Last-rendered product fields, so a live 'refresh' event can apply only the fields it carries a
// value for and keep everything else on screen (see mergeRefreshFields).
let lastData = null;
// Set when the user clicks Refresh; cleared by the matching 'refresh' WS event. Distinguishes a
// user-triggered refresh (toast on failure) from a background 3b refresh (silent).
let awaitingManualRefresh = false;
let ws = null;
const WS_RECONNECT_DELAY = 2000;

function api(path, opts = {}) {
    return fetch(path, {
        ...opts,
        headers: { 'X-API-Key': API_KEY, ...(opts.headers || {}) },
    });
}

function productPath() {
    return `/products/${encodeURIComponent(BARCODE)}/${encodeURIComponent(RETAILER_ID)}`;
}

function formatPrice(pence, type) {
    if (pence === null || pence === undefined) return null;
    const s = '£' + (pence / 100).toFixed(2);
    return type === 'per_kg' ? s + '/kg' : s;
}

function showError(msg) {
    document.getElementById('loading').classList.add('hidden');
    const err = document.getElementById('error-msg');
    err.textContent = msg;
    err.classList.remove('hidden');
}

function renderMemberships() {
    const list = document.getElementById('membership-list');
    if (currentGroups.length === 0) {
        list.innerHTML = '<li class="empty">Not in any group.</li>';
        return;
    }
    list.innerHTML = currentGroups.map(g => {
        const href = sanitiseHref(g.group_page_url);
        const label = href
            ? `<a href="${esc(href)}">${esc(g.name)}</a>`
            : esc(g.name);
        return `<li>${label}<button class="remove-btn" data-group-id="${g.id}">Remove</button></li>`;
    }).join('');
}

function renderGroupOptions() {
    const select = document.getElementById('group-select');
    const memberIds = new Set(currentGroups.map(g => g.id));
    const available = allGroups.filter(g => !memberIds.has(g.group_id));
    if (available.length === 0) {
        select.innerHTML = '<option value="">No other groups</option>';
        select.disabled = true;
        document.getElementById('add-group-btn').disabled = true;
        return;
    }
    select.disabled = false;
    document.getElementById('add-group-btn').disabled = false;
    select.innerHTML = available
        .map(g => `<option value="${g.group_id}">${esc(g.name)}</option>`)
        .join('');
}

function renderLinks(data) {
    const parts = [];
    const off = sanitiseHref(data.off_url);
    if (off) parts.push(`<a href="${esc(off)}" target="_blank" rel="noopener">OpenFoodFacts →</a>`);
    // Sainsbury's link only when a price exists (per spec).
    if (data.price_pence !== null && data.price_pence !== undefined) {
        const s = sanitiseHref(data.product_url);
        if (s) parts.push(`<a href="${esc(s)}" target="_blank" rel="noopener">Sainsbury's →</a>`);
    }
    document.getElementById('links').innerHTML = parts.join(' · ');
}

function render(data) {
    lastData = data;
    currentGroups = data.groups || [];

    const nameEl = document.getElementById('product-name');
    if (data.name) {
        nameEl.textContent = data.name;
    } else {
        // No lookup-status field yet: a null name may mean "OFF failed" or "never looked
        // up" — we can't tell, so use neutral wording, never "failed".
        nameEl.textContent = data.barcode;
        nameEl.classList.add('muted');
    }

    const subParts = [];
    if (data.brand) subParts.push(esc(data.brand));
    if (data.product_quantity) subParts.push(esc(data.product_quantity));
    const sub = document.getElementById('product-sub');
    if (subParts.length) {
        sub.innerHTML = subParts.join(' · ');
    } else {
        sub.innerHTML = '<span class="muted">Product details not yet available</span>';
    }

    document.getElementById('current-qty').textContent = data.current_quantity;
    const price = formatPrice(data.price_pence, data.price_type);
    document.getElementById('price').textContent = price || '—';
    document.getElementById('min-input').value = data.minimum_quantity;

    renderLinks(data);
    renderMemberships();
    renderGroupOptions();

    document.getElementById('loading').classList.add('hidden');
    document.getElementById('detail').hidden = false;
}

async function loadGroups() {
    try {
        const resp = await api('/groups');
        if (!resp.ok) return;
        allGroups = (await resp.json()).groups || [];
        renderGroupOptions();
    } catch (_) {
        // Non-fatal: the add-to-group control just stays empty.
    }
}

async function saveMinimum() {
    const raw = document.getElementById('min-input').value.trim();
    const val = parseInt(raw, 10);
    if (!Number.isInteger(val) || val < 0 || String(val) !== raw) {
        showToast('Minimum must be a whole number ≥ 0', 'error');
        return;
    }
    try {
        const resp = await api(`${productPath()}/minimum_quantity`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ minimum_quantity: val }),
        });
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        document.getElementById('min-input').value = val;
        showToast('Minimum saved ✓', 'ok');
    } catch (_) {
        showToast('Could not save minimum', 'error');
    }
}

async function addToGroup() {
    const select = document.getElementById('group-select');
    const groupId = parseInt(select.value, 10);
    if (!Number.isInteger(groupId)) return;
    try {
        const resp = await api(`${productPath()}/groups`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ group_id: groupId }),
        });
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        showToast('Added to group ✓', 'ok');
        await refresh();
    } catch (_) {
        showToast('Could not add to group', 'error');
    }
}

async function removeFromGroup(groupId) {
    try {
        const resp = await api(`${productPath()}/groups/${encodeURIComponent(groupId)}`, {
            method: 'DELETE',
        });
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        showToast('Removed from group ✓', 'ok');
        await refresh();
    } catch (_) {
        showToast('Could not remove from group', 'error');
    }
}

async function refresh() {
    const resp = await api(productPath());
    if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        if (resp.status === 404 && body.error === 'retailer_not_found') {
            showError('Unknown retailer.');
        } else if (resp.status === 404) {
            showError('Product not found.');
        } else {
            showError('Could not load product. Check connection.');
        }
        return;
    }
    render(await resp.json());
}

// --- Manual refresh (3c) -----------------------------------------------------
// FastAPI never calls OFF: the POST only marks the variant; the single worker performs the paced
// OFF + price refresh and writes the durable rows, and FastAPI's poll loop then broadcasts a
// 'refresh' WS event that this page applies live.

function setRefreshLoading(on) {
    const btn = document.getElementById('refresh-btn');
    if (!btn) return;
    btn.disabled = on;
    btn.classList.toggle('is-refreshing', on);
    btn.textContent = on ? 'Refreshing…' : 'Refresh';
}

async function requestManualRefresh() {
    awaitingManualRefresh = true;
    setRefreshLoading(true);
    try {
        const resp = await api(`${productPath()}/refresh`, { method: 'POST' });
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        // Keep the loading state + flag until the matching 'refresh' WS event arrives.
    } catch (_) {
        awaitingManualRefresh = false;
        setRefreshLoading(false);
        showToast('Could not start refresh', 'error');
    }
}

// Does this WS message target the product currently on the page?
function refreshEventMatches(msg, barcode, retailerId) {
    return !!msg && msg.type === 'refresh'
        && rowKey(msg.barcode, msg.retailer) === rowKey(barcode, retailerId);
}

// Decide what to do with a matching 'refresh' event. Pure — no DOM. Returns {apply, toast}:
//   - awaiting + OFF resolved  -> apply the new values (success; a price sub-failure is still a
//     success — the non-null merge simply keeps the old price).
//   - awaiting + OFF failed    -> do NOT apply; toast "keeping old values".
//   - not awaiting (background) -> apply silently, no toast.
// The caller clears awaitingManualRefresh whenever it was set (we consumed this event for it).
function decideRefreshAction(msg, awaiting) {
    if (awaiting) {
        if (msg.off_status === 'resolved') return { apply: true, toast: null };
        if (msg.off_status === 'failed') {
            return { apply: false, toast: 'Refresh failed — keeping old values' };
        }
        return { apply: false, toast: null };   // 'loading' — not expected on a terminal refresh
    }
    return { apply: true, toast: null };
}

// Merge a 'refresh' event's fields over the current data, applying each ONLY when the event carries
// a non-null value for it — so a value-gated null (OFF/price sub-failure) never blanks data that is
// still validly cached and displayed. Pure — no DOM. Note the wire field is `quantity` (1a), mapped
// back onto product_quantity for the render path.
function mergeRefreshFields(current, msg) {
    const merged = Object.assign({}, current || {});
    if (msg.name != null) merged.name = msg.name;
    if (msg.brand != null) merged.brand = msg.brand;
    if (msg.quantity != null) merged.product_quantity = msg.quantity;
    if (msg.price_pence != null) {
        merged.price_pence = msg.price_pence;
        merged.price_type = msg.price_type;
    }
    if (msg.off_url != null) merged.off_url = msg.off_url;
    if (msg.product_url != null) merged.product_url = msg.product_url;
    return merged;
}

// Apply a matching 'refresh' event to the page in place (name, brand·quantity subline, price, links).
// Does NOT touch on-hand quantity, minimum, or group membership — the event carries none of those.
function applyRefreshEvent(msg) {
    lastData = mergeRefreshFields(lastData, msg);

    const nameEl = document.getElementById('product-name');
    if (lastData.name) {
        nameEl.textContent = lastData.name;
        nameEl.classList.remove('muted');
    }

    const subParts = [];
    if (lastData.brand) subParts.push(esc(lastData.brand));
    if (lastData.product_quantity) subParts.push(esc(lastData.product_quantity));
    if (subParts.length) {
        document.getElementById('product-sub').innerHTML = subParts.join(' · ');
    }

    const price = formatPrice(lastData.price_pence, lastData.price_type);
    document.getElementById('price').textContent = price || '—';

    renderLinks(lastData);
}

function handleRefreshMessage(msg) {
    if (!refreshEventMatches(msg, BARCODE, RETAILER_ID)) return;
    setRefreshLoading(false);
    const wasAwaiting = awaitingManualRefresh;
    awaitingManualRefresh = false;   // consumed — a chained second (price) event is a background update
    const action = decideRefreshAction(msg, wasAwaiting);
    if (action.apply) applyRefreshEvent(msg);
    if (action.toast) showToast(action.toast, 'error');
}

function connectWS() {
    ws = new WebSocket(`ws://${window.location.host}/ws`);
    ws.onmessage = (event) => {
        let msg;
        try {
            msg = JSON.parse(event.data);
        } catch (_) {
            return;
        }
        // Only 'refresh' events concern this page; every other type (scan/resolution/delta_update)
        // is silently ignored.
        if (msg && msg.type === 'refresh') handleRefreshMessage(msg);
    };
    ws.onclose = () => { setTimeout(connectWS, WS_RECONNECT_DELAY); };
}

// --- Wiring (browser only; guarded so the module can be required in node --test) -----------------
if (typeof document !== 'undefined') {
    // Event delegation for per-row remove buttons (the list re-renders on every change).
    document.getElementById('membership-list').addEventListener('click', e => {
        const btn = e.target.closest('.remove-btn');
        if (btn) removeFromGroup(btn.dataset.groupId);
    });
    document.getElementById('min-save').addEventListener('click', saveMinimum);
    document.getElementById('add-group-btn').addEventListener('click', addToGroup);
    document.getElementById('refresh-btn').addEventListener('click', requestManualRefresh);

    (async function init() {
        if (!BARCODE) {
            showError('No barcode specified.');
            return;
        }
        try {
            await refresh();
            await loadGroups();
            connectWS();
        } catch (_) {
            showError('Could not load product. Check connection.');
        }
    })();
}

// Exported for the Node test runner (`node --test frontend/product.test.js`); ignored in the browser.
if (typeof module !== 'undefined') {
    module.exports = { refreshEventMatches, decideRefreshAction, mergeRefreshFields };
}
