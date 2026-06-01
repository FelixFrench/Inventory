function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function makeBadge(label) {
  const span = document.createElement('span');
  span.className = `badge badge--${label}`;
  span.textContent = label.replace(/-/g, ' ');
  return span;
}

function renderField(field) {
  if (field.label === 'resolved') {
    const span = document.createElement('span');
    span.textContent = field.value;
    return span;
  }
  return makeBadge(field.label);
}

function renderPrice(field) {
  if (field.label === 'resolved') {
    const span = document.createElement('span');
    span.textContent = '£' + field.value.toFixed(2);
    return span;
  }
  return makeBadge(field.label);
}

function renderTable(items) {
  const table = document.createElement('table');

  const thead = document.createElement('thead');
  thead.innerHTML = `<tr>
    <th>Product</th>
    <th class="col-brand">Brand</th>
    <th class="col-weight">Weight</th>
    <th>Price</th>
    <th>In Stock</th>
    <th>Session</th>
  </tr>`;
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  for (const item of items) {
    const tr = document.createElement('tr');

    // Product cell: name if resolved, else barcode as fallback
    const tdProduct = document.createElement('td');
    if (item.name.label === 'resolved') {
      tdProduct.textContent = item.name.value;
    } else {
      const code = document.createElement('span');
      code.className = 'barcode-fallback';
      code.textContent = item.barcode;
      tdProduct.appendChild(code);
    }
    tr.appendChild(tdProduct);

    // Brand
    const tdBrand = document.createElement('td');
    tdBrand.className = 'col-brand';
    tdBrand.appendChild(renderField(item.brand));
    tr.appendChild(tdBrand);

    // Weight
    const tdWeight = document.createElement('td');
    tdWeight.className = 'col-weight';
    tdWeight.appendChild(renderField(item.weight));
    tr.appendChild(tdWeight);

    // Price
    const tdPrice = document.createElement('td');
    tdPrice.appendChild(renderPrice(item.price));
    tr.appendChild(tdPrice);

    // In Stock
    const tdStock = document.createElement('td');
    tdStock.textContent = item.inventory_quantity;
    tr.appendChild(tdStock);

    // Session
    const tdSession = document.createElement('td');
    if (item.in_active_session) {
      tdSession.appendChild(makeBadge('active-session'));
      tdSession.querySelector('.badge--active-session').textContent = 'Active';
    }
    tr.appendChild(tdSession);

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

document.addEventListener('DOMContentLoaded', function () {
  const container = document.getElementById('report-container');
  container.textContent = 'Loading…';

  fetch('/reports/unresolved', {
    headers: { 'X-API-Key': API_KEY },
  })
    .then(function (resp) {
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return resp.json();
    })
    .then(function (data) {
      container.textContent = '';
      if (data.total_count === 0) {
        const p = document.createElement('p');
        p.className = 'status-msg';
        p.textContent = 'All products are fully resolved.';
        container.appendChild(p);
      } else {
        container.appendChild(renderTable(data.items));
      }
    })
    .catch(function () {
      container.textContent = '';
      const p = document.createElement('p');
      p.className = 'status-msg';
      p.textContent = 'Failed to load report. Please try again.';
      container.appendChild(p);
    });
});
