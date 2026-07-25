// Groups list / management page.
// esc(), sanitiseHref(), showToast() live in shared-utils.js (loaded first).

function api(path, opts = {}) {
    return fetch(path, {
        ...opts,
        headers: { 'X-API-Key': API_KEY, ...(opts.headers || {}) },
    });
}

function showError(msg) {
    document.getElementById('loading').classList.add('hidden');
    const err = document.getElementById('error-msg');
    err.textContent = msg;
    err.classList.remove('hidden');
}

function groupsTable(groups) {
    if (!groups.length) {
        return '<p class="empty">No groups yet. Create one above.</p>';
    }
    const rows = groups.map(g => {
        const href = sanitiseHref(g.group_page_url);
        const name = href
            ? `<a href="${esc(href)}">${esc(g.name)}</a>`
            : esc(g.name);
        const min = g.minimum_quantity === 0 ? '—' : g.minimum_quantity;
        const short = (g.minimum_quantity !== 0 && g.low_stock)
            ? `<span class="shortfall-badge">${g.shortfall}</span>`
            : '<span class="ok-dash">—</span>';
        return `
            <tr>
              <td>${name}</td>
              <td>${g.total_quantity}</td>
              <td>${min}</td>
              <td class="shortfall-col">${short}</td>
            </tr>`;
    }).join('');
    return `
        <table>
          <thead>
            <tr>
              <th>Group</th>
              <th>Have</th>
              <th>Need</th>
              <th class="shortfall-col">Short</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>`;
}

function render(groups) {
    const el = document.getElementById('content');
    el.innerHTML = groupsTable(groups);
    document.getElementById('loading').classList.add('hidden');
    el.hidden = false;
}

async function refresh() {
    const resp = await api('/groups');
    if (!resp.ok) throw new Error('Server error ' + resp.status);
    render((await resp.json()).groups || []);
}

async function createGroup(e) {
    e.preventDefault();
    const nameEl = document.getElementById('new-name');
    const minEl = document.getElementById('new-min');
    const name = nameEl.value.trim();
    if (!name) { showToast('Name cannot be empty', 'error'); return; }
    const raw = minEl.value.trim() || '0';
    const min = parseInt(raw, 10);
    if (!Number.isInteger(min) || min < 0 || String(min) !== raw) {
        showToast('Minimum must be a whole number ≥ 0', 'error');
        return;
    }
    try {
        const resp = await api('/groups', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, minimum_quantity: min }),
        });
        if (resp.status === 409) { showToast('A group with that name already exists', 'error'); return; }
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        nameEl.value = '';
        minEl.value = '0';
        showToast('Group created ✓', 'ok');
        await refresh();
    } catch (_) {
        showToast('Could not create group', 'error');
    }
}

async function init() {
    try {
        await refresh();
    } catch (_) {
        showError('Could not load groups. Check connection.');
    }
}

// Guarded so the module can be required by the Node test runner, where `document` is
// undefined; in a browser the guard is always true and the load-time wiring is unchanged.
if (typeof document !== 'undefined') {
    document.getElementById('create-form').addEventListener('submit', createGroup);
    init();
}

// Exported for the Node test runner (`node --test frontend/groups.test.js`); ignored in the
// browser, where `module` is undefined and these are plain globals.
if (typeof module !== 'undefined') {
    module.exports = { groupsTable };
}
