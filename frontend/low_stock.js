// esc() lives in shared-utils.js (loaded before this script).

function groupsTable(groups) {
  if (groups.length === 0) {
    return '<p class="empty">No groups below minimum.</p>';
  }
  const rows = groups.map(g => {
    const nameText = esc(g.name) || '—';
    const href = sanitiseHref(g.group_page_url);
    const nameCell = href
      ? `<td class="cell-link"><a href="${esc(href)}"><div>${nameText}</div></a></td>`
      : `<td><div>${nameText}</div></td>`;
    return `
          <tr>
            ${nameCell}
            <td>${g.have}</td>
            <td>${g.need}</td>
            <td class="shortfall-col"><span class="shortfall-badge">${g.short}</span></td>
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

function productsTable(products) {
  if (products.length === 0) {
    return '<p class="empty">No products below minimum.</p>';
  }
  const rows = products.map(item => {
    const nameText = esc(item.name) || '—';
    const brandHtml = item.brand ? '<div class="brand">' + esc(item.brand) + '</div>' : '';
    const href = sanitiseHref(item.product_page_url);
    const nameCell = href
      ? `<td class="cell-link"><a href="${esc(href)}"><div>${nameText}</div>${brandHtml}</a></td>`
      : `<td><div>${nameText}</div>${brandHtml}</td>`;
    return `
          <tr>
            ${nameCell}
            <td>${item.have}</td>
            <td>${item.need}</td>
            <td class="shortfall-col"><span class="shortfall-badge">${item.short}</span></td>
          </tr>`;
  }).join('');
  return `
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

function render(data) {
  const el = document.getElementById('content');
  el.innerHTML = `
        <section class="low-stock-section">
          <h2>Groups</h2>
          ${groupsTable(data.groups)}
        </section>
        <section class="low-stock-section">
          <h2>Products</h2>
          ${productsTable(data.products)}
        </section>`;
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

// showToast() lives in shared-utils.js (loaded before this script).

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
