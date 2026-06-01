function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Track the currently active inline edit so only one row is open at a time.
let activeEdit = null;

function cancelActiveEdit() {
  if (!activeEdit) return;
  const { wrapper, span } = activeEdit;
  if (wrapper.parentNode) wrapper.replaceWith(span);
  activeEdit = null;
}

function startEdit(span) {
  cancelActiveEdit();

  const barcode = span.dataset.barcode;
  const originalValue = parseInt(span.textContent, 10);

  const input = document.createElement('input');
  input.type = 'number';
  input.min = '0';
  input.value = originalValue;
  input.className = 'min-qty-input';

  const saveBtn = document.createElement('button');
  saveBtn.textContent = 'Save';
  saveBtn.className = 'min-qty-save';

  const errSpan = document.createElement('span');
  errSpan.className = 'min-qty-err';

  const wrapper = document.createElement('span');
  wrapper.className = 'min-qty-edit';
  wrapper.appendChild(input);
  wrapper.appendChild(saveBtn);
  wrapper.appendChild(errSpan);

  span.replaceWith(wrapper);
  input.focus();
  input.select();

  activeEdit = { wrapper, span, barcode };

  // Guard: once commit() is called it sets this to true so blur/click/Enter
  // cannot trigger a second concurrent request. Reset to false on error to allow retry.
  let committed = false;

  async function commit() {
    if (committed) return;
    committed = true;

    const raw = input.value.trim();
    const val = parseInt(raw, 10);
    if (!Number.isInteger(val) || val < 0 || raw === '' || String(val) !== raw) {
      // Invalid — silently restore the original value.
      wrapper.replaceWith(span);
      if (activeEdit && activeEdit.wrapper === wrapper) activeEdit = null;
      return;
    }

    errSpan.textContent = '';
    try {
      const resp = await fetch('/products/' + encodeURIComponent(barcode) + '/minimum_quantity', {
        method: 'PUT',
        headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' },
        body: JSON.stringify({ minimum_quantity: val }),
      });
      if (!resp.ok) throw new Error('Server error ' + resp.status);
      span.textContent = val;
      wrapper.replaceWith(span);
    } catch (_) {
      errSpan.textContent = 'Save failed';
      committed = false; // allow the user to retry
      return;
    }

    if (activeEdit && activeEdit.wrapper === wrapper) activeEdit = null;
  }

  function cancel() {
    if (committed) return;
    committed = true; // prevent any pending blur from firing commit
    wrapper.replaceWith(span);
    if (activeEdit && activeEdit.wrapper === wrapper) activeEdit = null;
  }

  // mousedown preventDefault keeps focus on the input when tapping Save,
  // so blur fires AFTER the click handler runs rather than before it.
  saveBtn.addEventListener('mousedown', e => e.preventDefault());
  saveBtn.addEventListener('click', commit);
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
  });
}

function renderProducts(products) {
  const list = document.getElementById('product-list');
  list.innerHTML = '';

  if (products.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = 'No products in inventory yet.';
    list.parentNode.insertBefore(empty, list);
    return;
  }

  for (const p of products) {
    const li = document.createElement('li');

    // Name line
    const nameDiv = document.createElement('div');
    nameDiv.className = 'product-name';
    if (p.name) {
      nameDiv.textContent = p.name;
    } else {
      nameDiv.innerHTML = '<em class="muted">Unknown (' + esc(p.barcode) + ')</em>';
    }
    li.appendChild(nameDiv);

    // Brand + weight line (omit if both null)
    if (p.brand || p.weight) {
      const metaDiv = document.createElement('div');
      metaDiv.className = 'product-meta';
      const parts = [p.brand, p.weight].filter(Boolean).map(esc);
      metaDiv.textContent = parts.join(' · ');
      li.appendChild(metaDiv);
    }

    // Stock line
    const stockDiv = document.createElement('div');
    stockDiv.className = 'product-meta';
    stockDiv.textContent = p.current_quantity + ' in stock';
    li.appendChild(stockDiv);

    // Minimum quantity row
    const minRow = document.createElement('div');
    minRow.className = 'min-row';
    const minLabel = document.createElement('span');
    minLabel.className = 'min-label';
    minLabel.textContent = 'Minimum: ';
    const minSpan = document.createElement('span');
    minSpan.className = 'min-qty-value';
    minSpan.dataset.barcode = p.barcode;
    minSpan.textContent = p.minimum_quantity;
    minRow.appendChild(minLabel);
    minRow.appendChild(minSpan);
    li.appendChild(minRow);

    list.appendChild(li);
  }
}

// Event delegation: single listener on the list container.
document.getElementById('product-list').addEventListener('click', e => {
  const span = e.target.closest('.min-qty-value');
  if (span) startEdit(span);
});

(async function init() {
  try {
    const resp = await fetch('/products/minimum-quantities', {
      headers: { 'X-API-Key': API_KEY },
    });
    if (!resp.ok) throw new Error('Server error ' + resp.status);
    const data = await resp.json();
    document.getElementById('loading-msg').hidden = true;
    renderProducts(data.products);
    document.getElementById('product-list').hidden = false;
  } catch (_) {
    document.getElementById('loading-msg').hidden = true;
    const err = document.getElementById('error-msg');
    err.textContent = 'Could not load minimum quantities. Check connection.';
    err.hidden = false;
  }
})();
