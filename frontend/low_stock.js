// esc() lives in shared-utils.js (loaded before this script).

function render(data) {
  const el = document.getElementById('content');
  if (data.items.length === 0) {
    el.innerHTML = '<p class="empty">All items are sufficiently stocked.</p>';
  } else {
    const rows = data.items.map(item => `
          <tr>
            <td>
              <div>${esc(item.name) || '—'}</div>
              ${item.brand ? '<div class="brand">' + esc(item.brand) + '</div>' : ''}
            </td>
            <td>${item.quantity}</td>
            <td>${item.minimum_quantity}</td>
            <td class="shortfall-col"><span class="shortfall-badge">${item.shortfall}</span></td>
          </tr>`).join('');
    el.innerHTML = `
          <table>
            <thead>
              <tr>
                <th>Item</th>
                <th>Have</th>
                <th>Need</th>
                <th class="shortfall-col">Short</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>`;
  }
  document.getElementById('loading').classList.add('hidden');
  el.classList.remove('hidden');
}

(async function init() {
  try {
    const resp = await fetch('/reports/low-stock', { headers: { 'X-API-Key': API_KEY } });
    if (!resp.ok) throw new Error('Server error ' + resp.status);
    render(await resp.json());
  } catch (e) {
    document.getElementById('loading').classList.add('hidden');
    const err = document.getElementById('error-msg');
    err.textContent = 'Could not load low stock report. Check connection.';
    err.classList.remove('hidden');
  }
})();

let _activeToast = null;

function showToast(message, type) {
    if (_activeToast) {
        _activeToast.remove();
        _activeToast = null;
    }
    const toast = document.createElement('div');
    toast.className = `toast toast--${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    _activeToast = toast;
    setTimeout(() => {
        toast.remove();
        if (_activeToast === toast) _activeToast = null;
    }, 4000);
}

document.getElementById('print-btn').addEventListener('click', async () => {
    const btn = document.getElementById('print-btn');
    const report = btn.dataset.report;

    btn.disabled = true;

    try {
        const res = await fetch(`/print/${report}`, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY }
        });
        if (res.ok) {
            showToast('Sent to printer ✓', 'ok');
        } else {
            const body = await res.json().catch(() => ({}));
            const msg = body.error === 'printer_not_configured'
                ? 'Printer not configured'
                : 'Printer unavailable';
            showToast(msg, 'error');
        }
    } catch {
        showToast('Printer unavailable', 'error');
    } finally {
        btn.disabled = false;
    }
});
