// Tests for product.js's pure refresh-event decision helpers (Sprint 2, Phase 3c).
// Run: node --test frontend/product.test.js

const test = require('node:test');
const assert = require('node:assert');

// product.js reads window.location at import (to parse ?barcode/?retailer_id) and its refresh
// matcher uses the global rowKey from shared-utils.js — stub both before requiring it. The DOM
// wiring in product.js is guarded by `typeof document !== 'undefined'`, so it stays inert here.
globalThis.window = { location: { search: '', origin: 'https://app.example', host: 'app.example' } };
globalThis.rowKey = require('./shared-utils.js').rowKey;

const { refreshEventMatches, decideRefreshAction, mergeRefreshFields } = require('./product.js');

// --- refreshEventMatches -----------------------------------------------------

test('refreshEventMatches: matches same barcode/retailer, coercing string vs int', () => {
    const msg = { type: 'refresh', barcode: '5014788110140', retailer: 1 };
    assert.strictEqual(refreshEventMatches(msg, '5014788110140', '1'), true);
});

test('refreshEventMatches: rejects a different barcode', () => {
    const msg = { type: 'refresh', barcode: '0000000000000', retailer: 1 };
    assert.strictEqual(refreshEventMatches(msg, '5014788110140', '1'), false);
});

test('refreshEventMatches: rejects a different retailer', () => {
    const msg = { type: 'refresh', barcode: '5014788110140', retailer: 2 };
    assert.strictEqual(refreshEventMatches(msg, '5014788110140', '1'), false);
});

test('refreshEventMatches: rejects a non-refresh type', () => {
    const msg = { type: 'resolution', barcode: '5014788110140', retailer: 1 };
    assert.strictEqual(refreshEventMatches(msg, '5014788110140', '1'), false);
});

// --- decideRefreshAction -----------------------------------------------------

test('decideRefreshAction: awaiting + OFF resolved -> apply, no toast', () => {
    assert.deepStrictEqual(
        decideRefreshAction({ off_status: 'resolved' }, true),
        { apply: true, toast: null },
    );
});

test('decideRefreshAction: awaiting + OFF failed -> no apply, keep-old-values toast', () => {
    assert.deepStrictEqual(
        decideRefreshAction({ off_status: 'failed' }, true),
        { apply: false, toast: 'Refresh failed — keeping old values' },
    );
});

test('decideRefreshAction: not awaiting (background) resolved -> silent apply', () => {
    assert.deepStrictEqual(
        decideRefreshAction({ off_status: 'resolved' }, false),
        { apply: true, toast: null },
    );
});

test('decideRefreshAction: not awaiting (background) failed -> still silent apply, no toast', () => {
    // The non-null merge rule makes applying a failed background event safe (it blanks nothing).
    assert.deepStrictEqual(
        decideRefreshAction({ off_status: 'failed' }, false),
        { apply: true, toast: null },
    );
});

// --- mergeRefreshFields (item 1: apply only non-null fields) ------------------

const RESOLVED_EVENT = {
    off_status: 'resolved', price_status: 'resolved',
    name: 'Baked Beans', brand: 'Heinz', quantity: '415g',
    price_pence: 120, price_type: 'unit',
    off_url: 'https://off/p', product_url: 'https://sains/p',
};

test('mergeRefreshFields: a fully-resolved event applies every field', () => {
    const merged = mergeRefreshFields({}, RESOLVED_EVENT);
    assert.strictEqual(merged.name, 'Baked Beans');
    assert.strictEqual(merged.brand, 'Heinz');
    assert.strictEqual(merged.product_quantity, '415g');  // wire 'quantity' -> product_quantity
    assert.strictEqual(merged.price_pence, 120);
    assert.strictEqual(merged.price_type, 'unit');
    assert.strictEqual(merged.off_url, 'https://off/p');
    assert.strictEqual(merged.product_url, 'https://sains/p');
});

test('mergeRefreshFields: a price-failed event must NOT clear an existing price', () => {
    // The 30-day-refresh failure case: OFF resolved, price lookup failed -> price_pence null on the
    // wire, but the DB kept the old price. Applying must keep the displayed price.
    const current = { name: 'Baked Beans', price_pence: 99, price_type: 'unit', product_url: 'https://old' };
    const event = {
        off_status: 'resolved', price_status: 'failed',
        name: 'Baked Beans', brand: 'Heinz', quantity: '415g',
        price_pence: null, price_type: 'unit', off_url: 'https://off/p', product_url: null,
    };
    const merged = mergeRefreshFields(current, event);
    assert.strictEqual(merged.price_pence, 99);            // preserved, not blanked
    assert.strictEqual(merged.product_url, 'https://old'); // preserved
    assert.strictEqual(merged.brand, 'Heinz');             // applied
});

test('mergeRefreshFields: an OFF-failed event must NOT blank existing name/brand/quantity', () => {
    const current = { name: 'Baked Beans', brand: 'Heinz', product_quantity: '415g', price_pence: 120 };
    const event = {
        off_status: 'failed', price_status: 'resolved',
        name: null, brand: null, quantity: null,
        price_pence: 130, price_type: 'unit', off_url: 'https://off/p', product_url: 'https://s',
    };
    const merged = mergeRefreshFields(current, event);
    assert.strictEqual(merged.name, 'Baked Beans');   // kept
    assert.strictEqual(merged.brand, 'Heinz');        // kept
    assert.strictEqual(merged.product_quantity, '415g'); // kept
    assert.strictEqual(merged.price_pence, 130);      // price still applied
});

test('mergeRefreshFields: does not mutate the passed-in current object', () => {
    const current = { name: 'Old' };
    mergeRefreshFields(current, RESOLVED_EVENT);
    assert.strictEqual(current.name, 'Old');
});
