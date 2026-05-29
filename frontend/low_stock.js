function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

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
