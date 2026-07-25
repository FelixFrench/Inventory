// Unit tests for groups.js's pure table builder (the 2c groups page).
// Run: node --test frontend/groups.test.js   (the FILE, never the directory)
//
// Distinct from low_stock.js's same-named groupsTable: this one lists EVERY group, so it has
// the no-minimum and not-low branches that the low-stock table never sees.

const test = require('node:test');
const assert = require('node:assert');

globalThis.window = { location: { origin: 'https://app.example' } };
const shared = require('./shared-utils.js');
globalThis.esc = shared.esc;
globalThis.sanitiseHref = shared.sanitiseHref;

const { groupsTable } = require('./groups.js');

const GROUP = {
    name: 'Beans', total_quantity: 2, minimum_quantity: 5, low_stock: true, shortfall: 3,
    group_page_url: '/group.html?id=7',
};

test('groupsTable renders the empty-state prompt', () => {
    assert.strictEqual(groupsTable([]), '<p class="empty">No groups yet. Create one above.</p>');
});

test('groupsTable shows a shortfall badge for a low-stock group', () => {
    const html = groupsTable([GROUP]);
    assert.match(html, /<span class="shortfall-badge">3<\/span>/);
    assert.match(html, /<td>5<\/td>/);          // the minimum, rendered as a number
});

test('groupsTable shows an em dash for a group with no minimum set', () => {
    // minimum_quantity === 0 means "no minimum", not "minimum of zero".
    const html = groupsTable([{ ...GROUP, minimum_quantity: 0, low_stock: false, shortfall: 0 }]);
    assert.match(html, /<td>—<\/td>/);
    assert.doesNotMatch(html, /shortfall-badge/);
});

test('groupsTable suppresses the badge even if low_stock is set with a zero minimum', () => {
    // Both halves of `minimum_quantity !== 0 && low_stock` are load-bearing: a stale low_stock
    // flag must not produce a badge for a group that has no minimum.
    const html = groupsTable([{ ...GROUP, minimum_quantity: 0, low_stock: true, shortfall: 4 }]);
    assert.doesNotMatch(html, /shortfall-badge/);
    assert.match(html, /<span class="ok-dash">—<\/span>/);
});

test('groupsTable shows the ok dash for a group that is at or above its minimum', () => {
    const html = groupsTable([{ ...GROUP, total_quantity: 9, low_stock: false, shortfall: 0 }]);
    assert.match(html, /<span class="ok-dash">—<\/span>/);
});

test('groupsTable links the name when the URL passes sanitiseHref', () => {
    assert.match(groupsTable([GROUP]), /<a href="\/group\.html\?id=7">Beans<\/a>/);
});

test('groupsTable emits a bare escaped name when the URL is rejected', () => {
    const html = groupsTable([{ ...GROUP, group_page_url: 'javascript:alert(1)', name: '<b>x</b>' }]);
    assert.doesNotMatch(html, /<a /);
    assert.doesNotMatch(html, /javascript:/);
    assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
});
