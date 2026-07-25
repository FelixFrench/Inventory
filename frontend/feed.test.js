// Unit tests for feed.js's pure helpers.
// Run: node --test frontend/feed.test.js   (the FILE, never the directory)
//
// feed.js wires its DOM listener behind `typeof document !== 'undefined'`, so requiring it
// under Node is inert. renderField calls the global esc() from shared-utils.js, which is
// module-scoped under CommonJS — hoist it onto globalThis first, as product.test.js does.

const test = require('node:test');
const assert = require('node:assert');

globalThis.window = { location: { origin: 'https://app.example' } };
globalThis.esc = require('./shared-utils.js').esc;

const { overrideUrl, _statusMarkup, renderField, renderPrice } = require('./feed.js');

// --- overrideUrl (the 2e composite override endpoint) ------------------------

test('overrideUrl builds the composite /session/items/{barcode}/{retailer} path', () => {
    // A barcode-only path is not a 404 but a 405 (the static mount owns it), so a regression
    // here would look like a silently failing quantity edit rather than an obvious error.
    assert.strictEqual(
        overrideUrl({ barcode: '5014788110140', retailer: 1 }),
        '/session/items/5014788110140/1',
    );
});

test('overrideUrl distinguishes retailers for the same barcode', () => {
    assert.notStrictEqual(
        overrideUrl({ barcode: '5014788110140', retailer: 1 }),
        overrideUrl({ barcode: '5014788110140', retailer: 2 }),
    );
});

test('overrideUrl percent-encodes both components', () => {
    assert.strictEqual(
        overrideUrl({ barcode: 'a/b', retailer: 'x y' }),
        '/session/items/a%2Fb/x%20y',
    );
});

// --- _statusMarkup / renderField / renderPrice -------------------------------

test('_statusMarkup returns markup for loading and failed, null otherwise', () => {
    assert.strictEqual(_statusMarkup({ status: 'loading' }), '<span class="italic-muted">loading…</span>');
    assert.strictEqual(_statusMarkup({ status: 'failed' }), '<span class="muted">failed</span>');
    assert.strictEqual(_statusMarkup({ status: 'resolved', value: 'x' }), null);
});

test('renderField shows the status markup ahead of any value', () => {
    // A value-gated field arrives as {value: null, status: 'loading'}; the status wins.
    assert.strictEqual(
        renderField({ value: null, status: 'loading' }),
        '<span class="italic-muted">loading…</span>',
    );
});

test('renderField escapes a resolved value', () => {
    assert.strictEqual(
        renderField({ value: '<b>Beans</b>', status: 'resolved' }),
        '&lt;b&gt;Beans&lt;/b&gt;',
    );
});

test('renderField shows "unknown" for a resolved-but-null value', () => {
    assert.strictEqual(
        renderField({ value: null, status: 'resolved' }),
        '<span class="muted">unknown</span>',
    );
});

test('renderPrice formats a resolved price to two decimals', () => {
    assert.strictEqual(renderPrice({ value: 1.5, status: 'resolved' }), '£1.50');
    assert.strictEqual(renderPrice({ value: 0, status: 'resolved' }), '£0.00');
});

test('renderPrice shows "unknown" rather than £NaN for a null price', () => {
    assert.strictEqual(
        renderPrice({ value: null, status: 'resolved' }),
        '<span class="muted">unknown</span>',
    );
});

test('renderPrice shows the failed marker instead of touching the value', () => {
    assert.strictEqual(
        renderPrice({ value: null, status: 'failed' }),
        '<span class="muted">failed</span>',
    );
});
