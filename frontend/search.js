// Product-search page. No backend endpoint of its own: manual entry navigates to the
// product page, and a /ws scan (from anywhere on the LAN) redirects there too.
// esc() lives in shared-utils.js (loaded before this script).

// ── Navigation helpers ───────────────────────────────────────────────────────
// product.html is a static, known URL shape (retailer_id optional — it defaults to
// Sainsbury's), so building location.href here is not an off_url/product_page_url
// reconstruction and needs no sanitiseHref.

function goToProduct(barcode) {
    window.location.href = '/product.html?barcode=' + encodeURIComponent(barcode);
}

function goToProductWithRetailer(barcode, retailer) {
    window.location.href = '/product.html?barcode=' + encodeURIComponent(barcode)
        + '&retailer_id=' + encodeURIComponent(retailer);
}

// ── Manual entry ─────────────────────────────────────────────────────────────

function onSubmit(e) {
    e.preventDefault();
    const value = document.getElementById('search-input').value.trim();
    if (!value) return;
    const status = document.getElementById('search-status');
    // esc() the user-entered value: it is echoed into the page via innerHTML.
    status.innerHTML = 'Opening product page for <strong>' + esc(value) + '</strong>…';
    goToProduct(value);
}

// ── Scan listener ──────────────────────────────────────────────────────────────
// Any type:'scan' message (in-session or not) redirects this page to the scanned
// product. The redirect is intentionally not client-targeted: any open search page
// reacts to any scan system-wide.

let ws;
const RECONNECT_DELAY = 2000;

function connectWS() {
    ws = new WebSocket(`ws://${window.location.host}/ws`);
    ws.onmessage = (event) => {
        let msg;
        try {
            msg = JSON.parse(event.data);
        } catch (_) {
            return;
        }
        if (msg && msg.type === 'scan' && msg.barcode) {
            goToProductWithRetailer(msg.barcode, msg.retailer);
        }
    };
    ws.onclose = () => {
        setTimeout(connectWS, RECONNECT_DELAY);
    };
}

// ── Init ─────────────────────────────────────────────────────────────────────

document.getElementById('search-form').addEventListener('submit', onSubmit);
document.getElementById('search-input').focus();
connectWS();
