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
    const rows = data.items.map(item => `
          <tr>
            <td>
              <div>${esc(item.name) || '—'}</div>
              ${item.brand ? '<div class="brand">' + esc(item.brand) + '</div>' : ''}
            </td>
            <td>${item.quantity}</td>
            <td>${pence(item.price_pence)}</td>
            <td>${pence(item.line_total_pence)}</td>
          </tr>`).join('');
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
