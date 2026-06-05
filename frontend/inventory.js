function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function pence(p) {
  return p != null ? '£' + (p / 100).toFixed(2) : '—';
}

function render(data) {
  const el = document.getElementById('content');
  if (data.items.length === 0) {
    el.innerHTML = '<p class="empty">No items in inventory yet.</p>';
  } else {
    const rows = data.items.map(item => {
      const nameText = item.name || item.barcode;
      const nameHtml = `<a href="${esc(item.off_url)}" target="_blank" rel="noopener noreferrer">${esc(nameText)}</a>`;
      const priceText = pence(item.price_pence);
      const priceHtml = item.price_url
        ? `<a href="${esc(item.price_url)}" target="_blank" rel="noopener noreferrer">${priceText}</a>`
        : priceText;
      return `
          <tr>
            <td>
              <div>${nameHtml}</div>
              ${item.brand ? '<div class="brand">' + esc(item.brand) + '</div>' : ''}
            </td>
            <td>${item.quantity}</td>
            <td>${priceHtml}</td>
            <td>${pence(item.line_total_pence)}</td>
          </tr>`;
    }).join('');
    el.innerHTML = `
          <table>
            <thead>
              <tr>
                <th>Item</th>
                <th>Qty</th>
                <th>Unit price</th>
                <th>Line total</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
            <tfoot>
              <tr>
                <td colspan="3">Total inventory value</td>
                <td>${pence(data.total_value_pence)}</td>
              </tr>
            </tfoot>
          </table>`;
  }
  document.getElementById('loading').classList.add('hidden');
  el.classList.remove('hidden');
}

(async function init() {
  try {
    const resp = await fetch('/reports/inventory', { headers: { 'X-API-Key': API_KEY } });
    if (!resp.ok) throw new Error('Server error ' + resp.status);
    render(await resp.json());
  } catch (e) {
    document.getElementById('loading').classList.add('hidden');
    const err = document.getElementById('error-msg');
    err.textContent = 'Could not load inventory. Check connection.';
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
