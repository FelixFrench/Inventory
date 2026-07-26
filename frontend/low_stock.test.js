// Unit tests for low_stock.js's pure table builders (the 2b reshaped {groups, products} report).
// Run: node --test frontend/low_stock.test.js   (the FILE, never the directory)
//
// The page's load-time fetch and print wiring sit behind `typeof document !== 'undefined'`, so
// requiring the module under Node is inert. esc()/sanitiseHref() are browser globals from
// shared-utils.js — hoist them onto globalThis before the require.

const test = require('node:test');
const assert = require('node:assert');

globalThis.window = { location: { origin: 'https://app.example' } };
const shared = require('./shared-utils.js');
globalThis.esc = shared.esc;
globalThis.sanitiseHref = shared.sanitiseHref;

const { groupsTable, productsTable } = require('./low_stock.js');

const GROUP = { name: 'Beans', have: 2, need: 5, short: 3, group_page_url: '/group.html?id=7' };
const PRODUCT = {
    name: 'Baked Beans', brand: 'Heinz', have: 1, need: 4, short: 3,
    product_page_url: '/product.html?barcode=5014788110140&retailer_id=1',
};

// --- empty states ------------------------------------------------------------

test('groupsTable renders the empty message, not an empty table', () => {
    assert.strictEqual(groupsTable([]), '<p class="empty">No groups below minimum.</p>');
});

test('productsTable renders its own distinct empty message', () => {
    assert.strictEqual(productsTable([]), '<p class="empty">No products below minimum.</p>');
});

// --- link vs plain cell (the sanitiseHref branch) -----------------------------

test('groupsTable links the name when the URL passes sanitiseHref', () => {
    const html = groupsTable([GROUP]);
    assert.match(html, /<a href="\/group\.html\?id=7">/);
    assert.match(html, /class="cell-link"/);
});

test('groupsTable falls back to a plain cell when the URL is rejected', () => {
    // A javascript: URL is rejected by sanitiseHref, so no anchor may be emitted at all.
    const html = groupsTable([{ ...GROUP, group_page_url: 'javascript:alert(1)' }]);
    assert.doesNotMatch(html, /<a /);
    assert.doesNotMatch(html, /javascript:/);
    assert.match(html, /<td><div>Beans<\/div><\/td>/);
});

test('groupsTable emits no anchor when the URL is absent', () => {
    const html = groupsTable([{ ...GROUP, group_page_url: null }]);
    assert.doesNotMatch(html, /<a /);
});

test('productsTable links the name when the URL passes sanitiseHref', () => {
    const html = productsTable([PRODUCT]);
    assert.match(html, /<a href="\/product\.html\?barcode=5014788110140&amp;retailer_id=1">/);
});

test('productsTable falls back to a plain cell when the URL is rejected', () => {
    const html = productsTable([{ ...PRODUCT, product_page_url: 'javascript:alert(1)' }]);
    assert.doesNotMatch(html, /<a /);
    assert.doesNotMatch(html, /javascript:/);
});

// --- escaping and fallbacks ---------------------------------------------------

test('groupsTable escapes a hostile group name', () => {
    const html = groupsTable([{ ...GROUP, name: '<img src=x onerror=alert(1)>' }]);
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test('productsTable escapes the name and the brand', () => {
    const html = productsTable([{ ...PRODUCT, name: '<b>x</b>', brand: '<i>y</i>' }]);
    assert.doesNotMatch(html, /<b>|<i>/);
    assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
    assert.match(html, /&lt;i&gt;y&lt;\/i&gt;/);
});

test('productsTable shows an em dash for a null name and omits an absent brand', () => {
    const html = productsTable([{ ...PRODUCT, name: null, brand: null }]);
    assert.match(html, /<div>—<\/div>/);
    assert.doesNotMatch(html, /class="brand"/);
});

test('groupsTable renders one row per group with the have/need/short figures', () => {
    const html = groupsTable([GROUP, { ...GROUP, name: 'Pasta', have: 0, need: 2, short: 2 }]);
    assert.strictEqual((html.match(/<tr>/g) || []).length, 3);   // header + 2 body rows
    assert.match(html, /<td>2<\/td>\s*<td>5<\/td>/);
    assert.match(html, /shortfall-badge">3</);
});
