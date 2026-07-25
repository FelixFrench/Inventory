// Tests for product.js's pure refresh-event decision helpers (Sprint 2, Phase 3c).
// Run: node --test frontend/product.test.js

const test = require('node:test');
const assert = require('node:assert');

// product.js reads window.location at import (to parse ?barcode/?retailer_id) and its refresh
// matcher uses the global rowKey from shared-utils.js — stub both before requiring it. The DOM
// wiring in product.js is guarded by `typeof document !== 'undefined'`, so it stays inert here.
globalThis.window = { location: { search: '', origin: 'https://app.example', host: 'app.example' } };
globalThis.rowKey = require('./shared-utils.js').rowKey;

const {
    refreshEventMatches, decideRefreshAction, mergeRefreshFields,
    findSessionDelta, sessionEventMatches, sessionBannerText,
    formatPrice,
} = require('./product.js');

// --- formatPrice (the 2d per-kg label, client side) ---------------------------

test('formatPrice renders integer pence as pounds to two decimals', () => {
    assert.strictEqual(formatPrice(120, 'unit'), '£1.20');
    assert.strictEqual(formatPrice(5, 'unit'), '£0.05');
    assert.strictEqual(formatPrice(0, 'unit'), '£0.00');   // 0 is a price, not "no price"
});

test('formatPrice appends /kg only for the per_kg type', () => {
    // Per-kg items store a non-null £/kg price; the type is what makes the label correct.
    assert.strictEqual(formatPrice(250, 'per_kg'), '£2.50/kg');
    assert.strictEqual(formatPrice(250, 'unit'), '£2.50');
});

test('formatPrice returns null (not "£NaN") when there is no price', () => {
    assert.strictEqual(formatPrice(null, 'unit'), null);
    assert.strictEqual(formatPrice(undefined, 'per_kg'), null);
});

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

test('refreshEventMatches: rejects a null/undefined message without throwing', () => {
    // The !! guard: a malformed frame must be ignored, not crash the socket handler.
    assert.strictEqual(refreshEventMatches(null, '5014788110140', '1'), false);
    assert.strictEqual(refreshEventMatches(undefined, '5014788110140', '1'), false);
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

test('decideRefreshAction: awaiting + OFF still loading -> neither apply nor toast', () => {
    // The third arm. A non-terminal event must not be mistaken for a failure and must not
    // apply half-resolved values while the user waits.
    assert.deepStrictEqual(
        decideRefreshAction({ off_status: 'loading' }, true),
        { apply: false, toast: null },
    );
});

test('decideRefreshAction: awaiting + no off_status at all -> neither apply nor toast', () => {
    // Same arm reached by omission rather than by an explicit 'loading'.
    assert.deepStrictEqual(
        decideRefreshAction({}, true),
        { apply: false, toast: null },
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

test('mergeRefreshFields: a null current is treated as an empty object', () => {
    // lastData is null until the first REST load completes; a refresh arriving in that window
    // must not throw.
    const merged = mergeRefreshFields(null, RESOLVED_EVENT);
    assert.strictEqual(merged.name, 'Baked Beans');
    assert.strictEqual(merged.price_pence, 120);
});

test('mergeRefreshFields: price_type travels with price_pence, not independently', () => {
    // price_type is only assigned inside the price_pence != null branch, so a per_kg tag can
    // never be applied on top of a preserved older price.
    const current = { price_pence: 99, price_type: 'unit' };
    const merged = mergeRefreshFields(current, { price_pence: null, price_type: 'per_kg' });
    assert.strictEqual(merged.price_pence, 99);
    assert.strictEqual(merged.price_type, 'unit');
});

// --- findSessionDelta -----------------------------------------------------

test('findSessionDelta: no active session -> 0', () => {
    assert.strictEqual(findSessionDelta(null, '5014788110140', '1'), 0);
});

test('findSessionDelta: active session, variant not yet scanned -> 0', () => {
    const session = { type: 'out', items: [{ barcode: '0000000000000', retailer: 1, delta: 3 }] };
    assert.strictEqual(findSessionDelta(session, '5014788110140', '1'), 0);
});

test('findSessionDelta: active session, variant present -> its delta, coercing string vs int', () => {
    const session = { type: 'out', items: [{ barcode: '5014788110140', retailer: 1, delta: 3 }] };
    assert.strictEqual(findSessionDelta(session, '5014788110140', '1'), 3);
});

test('findSessionDelta: a session object with no items key -> 0', () => {
    // A freshly started session serialises with an empty/absent item list; the `|| []`
    // fallback is what keeps this from throwing on .find.
    assert.strictEqual(findSessionDelta({ type: 'in' }, '5014788110140', '1'), 0);
    assert.strictEqual(findSessionDelta({ type: 'in', items: [] }, '5014788110140', '1'), 0);
});

test('findSessionDelta: distinguishes the same barcode under another retailer', () => {
    const session = { type: 'in', items: [{ barcode: '5014788110140', retailer: 2, delta: 9 }] };
    assert.strictEqual(findSessionDelta(session, '5014788110140', '1'), 0);
});

// --- sessionEventMatches --------------------------------------------------

test('sessionEventMatches: matches an in-session scan for this variant', () => {
    const msg = { type: 'scan', barcode: '5014788110140', retailer: 1, in_session: true, session_delta: 2 };
    assert.strictEqual(sessionEventMatches(msg, '5014788110140', '1'), true);
});

test('sessionEventMatches: rejects a sessionless scan (no session_delta)', () => {
    const msg = { type: 'scan', barcode: '5014788110140', retailer: 1, in_session: false };
    assert.strictEqual(sessionEventMatches(msg, '5014788110140', '1'), false);
});

test('sessionEventMatches: matches a delta_update for this variant', () => {
    const msg = { type: 'delta_update', barcode: '5014788110140', retailer: 1, session_delta: 0 };
    assert.strictEqual(sessionEventMatches(msg, '5014788110140', '1'), true);
});

test('sessionEventMatches: matches a resolution carrying session_delta', () => {
    const msg = { type: 'resolution', barcode: '5014788110140', retailer: 1, session_delta: 4 };
    assert.strictEqual(sessionEventMatches(msg, '5014788110140', '1'), true);
});

test('sessionEventMatches: rejects a different barcode', () => {
    const msg = { type: 'delta_update', barcode: '0000000000000', retailer: 1, session_delta: 1 };
    assert.strictEqual(sessionEventMatches(msg, '5014788110140', '1'), false);
});

test('sessionEventMatches: rejects a different retailer', () => {
    const msg = { type: 'delta_update', barcode: '5014788110140', retailer: 2, session_delta: 1 };
    assert.strictEqual(sessionEventMatches(msg, '5014788110140', '1'), false);
});

test('sessionEventMatches: rejects an unrelated type (refresh)', () => {
    const msg = { type: 'refresh', barcode: '5014788110140', retailer: 1 };
    assert.strictEqual(sessionEventMatches(msg, '5014788110140', '1'), false);
});

test('sessionEventMatches: rejects a null message without throwing', () => {
    assert.strictEqual(sessionEventMatches(null, '5014788110140', '1'), false);
});

test('sessionEventMatches: a scan omitting in_session is accepted when it carries a delta', () => {
    // The guard is a strict `in_session === false`, so `undefined` does not reject. The
    // session_delta check is the real second gate — documented here because feed.js uses the
    // same strict comparison on the opposite side of the same wire contract.
    const msg = { type: 'scan', barcode: '5014788110140', retailer: 1, session_delta: 2 };
    assert.strictEqual(sessionEventMatches(msg, '5014788110140', '1'), true);
});

test('sessionEventMatches: a delta of 0 is a real value, not a missing one', () => {
    // A [−] taking the delta back to zero must still update the banner; only undefined/null
    // count as absent.
    const scan = { type: 'scan', barcode: '5014788110140', retailer: 1, in_session: true, session_delta: 0 };
    assert.strictEqual(sessionEventMatches(scan, '5014788110140', '1'), true);
    const missing = { type: 'delta_update', barcode: '5014788110140', retailer: 1, session_delta: null };
    assert.strictEqual(sessionEventMatches(missing, '5014788110140', '1'), false);
});

// --- sessionBannerText -----------------------------------------------------

test('sessionBannerText: formats "N in current session (in|out)"', () => {
    assert.strictEqual(sessionBannerText(3, 'out'), '3 in current session (out)');
    assert.strictEqual(sessionBannerText(0, 'in'), '0 in current session (in)');
});

test('sessionBannerText: the no-session state stringifies rather than throwing', () => {
    // renderSessionBanner() still calls this with sessionType === null before hiding the
    // banner, so the null render must be harmless — the element is never visible in that state.
    assert.strictEqual(sessionBannerText(0, null), '0 in current session (null)');
});
