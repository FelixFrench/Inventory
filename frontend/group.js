// Group-info page. Addressed /group.html?id=...
// esc(), sanitiseHref(), showToast() live in shared-utils.js (loaded first).

const params = new URLSearchParams(window.location.search);
const GID = params.get('id') || '';

let allGroups = [];        // every group from GET /groups, for the add-sub-group dropdown
let currentSubgroups = []; // this group's direct sub-groups, for the dropdown exclusion set

function api(path, opts = {}) {
    return fetch(path, {
        ...opts,
        headers: { 'X-API-Key': API_KEY, ...(opts.headers || {}) },
    });
}

function groupPath() {
    return `/groups/${encodeURIComponent(GID)}`;
}

function showError(msg) {
    document.getElementById('loading').classList.add('hidden');
    const err = document.getElementById('error-msg');
    err.textContent = msg;
    err.classList.remove('hidden');
}

function renderVariants(variants) {
    const list = document.getElementById('variant-list');
    if (!variants.length) {
        list.innerHTML = '<li class="empty">No product members.</li>';
        return;
    }
    list.innerHTML = variants.map(v => {
        const href = sanitiseHref(v.product_page_url);
        const name = v.name ? esc(v.name) : `<span class="muted">${esc(v.barcode)}</span>`;
        const label = href ? `<a href="${esc(href)}">${name}</a>` : name;
        const meta = `<span class="member-meta">${v.current_quantity} on hand</span>`;
        return `<li><span class="member-main">${label}${meta}</span>`
            + `<button class="remove-btn" data-barcode="${esc(v.barcode)}">Remove</button></li>`;
    }).join('');
}

function renderSubgroups(subgroups) {
    const list = document.getElementById('subgroup-list');
    if (!subgroups.length) {
        list.innerHTML = '<li class="empty">No sub-groups.</li>';
        return;
    }
    list.innerHTML = subgroups.map(s => {
        const href = sanitiseHref(s.group_page_url);
        const label = href ? `<a href="${esc(href)}">${esc(s.name)}</a>` : esc(s.name);
        return `<li><span class="member-main">${label}</span>`
            + `<button class="remove-btn" data-child-id="${s.id}">Remove</button></li>`;
    }).join('');
}

function renderSubgroupOptions(subgroups) {
    const select = document.getElementById('subgroup-select');
    const childIds = new Set(subgroups.map(s => s.id));
    // Exclude this group itself and current children; the server still runs the
    // authoritative cycle check (409 cycle_detected) for deeper cycles.
    const available = allGroups.filter(
        g => String(g.group_id) !== String(GID) && !childIds.has(g.group_id),
    );
    if (!available.length) {
        select.innerHTML = '<option value="">No other groups</option>';
        select.disabled = true;
        document.getElementById('add-subgroup-btn').disabled = true;
        return;
    }
    select.disabled = false;
    document.getElementById('add-subgroup-btn').disabled = false;
    select.innerHTML = available
        .map(g => `<option value="${g.group_id}">${esc(g.name)}</option>`)
        .join('');
}

function render(data) {
    document.getElementById('name-input').value = data.name;
    document.getElementById('total-qty').textContent = data.total_quantity;
    document.getElementById('min-input').value = data.minimum_quantity;

    const status = document.getElementById('status-cell');
    if (data.minimum_quantity === 0) {
        status.innerHTML = '<span class="muted">No minimum</span>';
    } else if (data.low_stock) {
        status.innerHTML = `<span class="badge badge--low">Low · short ${data.shortfall}</span>`;
    } else {
        status.innerHTML = '<span class="badge badge--ok">OK</span>';
    }

    currentSubgroups = data.subgroups || [];
    renderVariants(data.variants || []);
    renderSubgroups(currentSubgroups);
    renderSubgroupOptions(currentSubgroups);

    document.getElementById('loading').classList.add('hidden');
    document.getElementById('detail').hidden = false;
}

async function loadGroups() {
    try {
        const resp = await api('/groups');
        if (!resp.ok) return;
        allGroups = (await resp.json()).groups || [];
    } catch (_) {
        // Non-fatal: the add-sub-group dropdown just stays empty.
    }
}

async function refresh() {
    const resp = await api(groupPath());
    if (!resp.ok) {
        if (resp.status === 404) showError('Group not found.');
        else showError('Could not load group. Check connection.');
        return;
    }
    render(await resp.json());
}

async function saveName() {
    const name = document.getElementById('name-input').value.trim();
    if (!name) {
        showToast('Name cannot be empty', 'error');
        return;
    }
    try {
        const resp = await api(groupPath(), {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        if (resp.status === 409) { showToast('A group with that name already exists', 'error'); return; }
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        showToast('Renamed ✓', 'ok');
    } catch (_) {
        showToast('Could not rename group', 'error');
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
        const resp = await api(groupPath(), {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ minimum_quantity: val }),
        });
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        showToast('Minimum saved ✓', 'ok');
        await refresh();
    } catch (_) {
        showToast('Could not save minimum', 'error');
    }
}

async function addVariant() {
    const input = document.getElementById('variant-barcode');
    const barcode = input.value.trim();
    if (!barcode) return;
    try {
        const resp = await api(`${groupPath()}/variants`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ barcode }),
        });
        if (resp.status === 404) { showToast('That barcode is not in the system', 'error'); return; }
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        input.value = '';
        showToast('Variant added ✓', 'ok');
        await refresh();
    } catch (_) {
        showToast('Could not add variant', 'error');
    }
}

async function removeVariant(barcode) {
    try {
        const resp = await api(`${groupPath()}/variants/${encodeURIComponent(barcode)}`, {
            method: 'DELETE',
        });
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        showToast('Variant removed ✓', 'ok');
        await refresh();
    } catch (_) {
        showToast('Could not remove product', 'error');
    }
}

async function addSubgroup() {
    const select = document.getElementById('subgroup-select');
    const childId = parseInt(select.value, 10);
    if (!Number.isInteger(childId)) return;
    try {
        const resp = await api(`${groupPath()}/subgroups`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ child_group_id: childId }),
        });
        if (resp.status === 409) { showToast('That would create a cycle', 'error'); return; }
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        showToast('Sub-group added ✓', 'ok');
        await refresh();
    } catch (_) {
        showToast('Could not add sub-group', 'error');
    }
}

async function removeSubgroup(childId) {
    try {
        const resp = await api(`${groupPath()}/subgroups/${encodeURIComponent(childId)}`, {
            method: 'DELETE',
        });
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        showToast('Sub-group removed ✓', 'ok');
        await refresh();
    } catch (_) {
        showToast('Could not remove sub-group', 'error');
    }
}

async function deleteGroup() {
    try {
        const resp = await api(groupPath(), { method: 'DELETE' });
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        window.location.href = '/groups.html';
    } catch (_) {
        document.getElementById('delete-modal').classList.add('hidden');
        showToast('Could not delete group', 'error');
    }
}

// Event delegation on the member lists (they re-render on every change).
document.getElementById('variant-list').addEventListener('click', e => {
    const btn = e.target.closest('.remove-btn');
    if (btn) removeVariant(btn.dataset.barcode);
});
document.getElementById('subgroup-list').addEventListener('click', e => {
    const btn = e.target.closest('.remove-btn');
    if (btn) removeSubgroup(btn.dataset.childId);
});
document.getElementById('name-save').addEventListener('click', saveName);
document.getElementById('min-save').addEventListener('click', saveMinimum);
document.getElementById('add-variant-btn').addEventListener('click', addVariant);
document.getElementById('add-subgroup-btn').addEventListener('click', addSubgroup);
document.getElementById('delete-btn').addEventListener('click', () =>
    document.getElementById('delete-modal').classList.remove('hidden'));
document.getElementById('cancel-delete').addEventListener('click', () =>
    document.getElementById('delete-modal').classList.add('hidden'));
document.getElementById('confirm-delete').addEventListener('click', deleteGroup);

(async function init() {
    if (!GID) {
        showError('No group specified.');
        return;
    }
    try {
        await refresh();
        await loadGroups();
        // allGroups is now populated; re-render the dropdown with the real option list.
        renderSubgroupOptions(currentSubgroups);
    } catch (_) {
        showError('Could not load group. Check connection.');
    }
})();
