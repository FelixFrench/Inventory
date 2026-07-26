// esc() lives in shared-utils.js (loaded before this script).

function makeBadge(label) {
  const span = document.createElement('span');
  // Backend labels use underscores (per_kg, not_attempted, no_data); the CSS badge
  // classes use hyphens (.badge--per-kg). Normalise so the styled badge actually applies.
  span.className = `badge badge--${label.replace(/_/g, '-')}`;
  span.textContent = label.replace(/[-_]/g, ' ');
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
    <th>Barcode</th>
    <th>Name</th>
    <th class="col-brand">Brand</th>
    <th class="col-quantity">Quantity</th>
    <th>Price</th>
    <th>In Stock</th>
  </tr>`;
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  for (const item of items) {
    const tr = document.createElement('tr');

    // The row's OFF link (external) — used on the name/brand/quantity cells.
    // Its view-vs-add-edit branching is baked into item.off_url server-side.
    const offHref = sanitiseHref(item.off_url);

    // Wrap a cell's content in the row's external OFF link, filling the cell.
    function offCell(td, content) {
      if (offHref) {
        td.classList.add('cell-link');
        const a = document.createElement('a');
        a.href = offHref;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.appendChild(content);
        td.appendChild(a);
      } else {
        td.appendChild(content);
      }
    }

    // Barcode — link to the internal product-info page (server-supplied)
    const tdBarcode = document.createElement('td');
    tdBarcode.className = 'barcode-fallback';
    const pageHref = sanitiseHref(item.product_page_url);
    if (pageHref) {
      tdBarcode.classList.add('cell-link');
      const barcodeLink = document.createElement('a');
      barcodeLink.href = pageHref;
      barcodeLink.textContent = item.barcode;
      tdBarcode.appendChild(barcodeLink);
    } else {
      tdBarcode.textContent = item.barcode;
    }
    tr.appendChild(tdBarcode);

    // Name — value or badge, linked to OFF
    const tdName = document.createElement('td');
    offCell(tdName, renderField(item.name));
    tr.appendChild(tdName);

    // Brand — linked to OFF
    const tdBrand = document.createElement('td');
    tdBrand.className = 'col-brand';
    offCell(tdBrand, renderField(item.brand));
    tr.appendChild(tdBrand);

    // Quantity — linked to OFF
    const tdQuantity = document.createElement('td');
    tdQuantity.className = 'col-quantity';
    offCell(tdQuantity, renderField(item.quantity));
    tr.appendChild(tdQuantity);

    // Price
    const tdPrice = document.createElement('td');
    const priceEl = renderPrice(item.price);
    const priceHref = sanitiseHref(item.price_url);
    if (priceHref) {
      tdPrice.classList.add('cell-link');
      const priceLink = document.createElement('a');
      priceLink.href = priceHref;
      priceLink.target = '_blank';
      priceLink.rel = 'noopener noreferrer';
      priceLink.appendChild(priceEl);
      tdPrice.appendChild(priceLink);
    } else {
      tdPrice.appendChild(priceEl);
    }
    tr.appendChild(tdPrice);

    // In Stock
    const tdStock = document.createElement('td');
    tdStock.textContent = item.inventory_quantity;
    tr.appendChild(tdStock);

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
