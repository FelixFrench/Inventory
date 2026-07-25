// Unit tests for shared-utils.js, run with the Node built-in test runner:
//   node --test frontend/shared-utils.test.js
// Point it at the FILE, never the directory (`node --test frontend/` fails MODULE_NOT_FOUND).
// No package.json / npm dependencies — shared-utils.js exports its functions via a
// guarded `module.exports` that the browser ignores.

const test = require('node:test');
const assert = require('node:assert');

// sanitiseHref() resolves relative URLs against window.location.origin, so the
// module needs a window global before it is required. Point it at a fixed origin.
globalThis.window = { location: { origin: 'https://app.example' } };

const { esc, sanitiseHref, rowKey } = require('./shared-utils.js');

test('sanitiseHref allows a same-origin relative path', () => {
    assert.strictEqual(
        sanitiseHref('/product.html?barcode=5014788110140&retailer_id=1'),
        '/product.html?barcode=5014788110140&retailer_id=1',
    );
});

test('sanitiseHref allows a same-origin absolute URL', () => {
    assert.strictEqual(
        sanitiseHref('https://app.example/group.html?id=7'),
        'https://app.example/group.html?id=7',
    );
});

test('sanitiseHref allows an external https URL (unchanged)', () => {
    assert.strictEqual(
        sanitiseHref('https://world.openfoodfacts.org/product/5014788110140'),
        'https://world.openfoodfacts.org/product/5014788110140',
    );
});

test('sanitiseHref rejects an external http URL (unchanged)', () => {
    assert.strictEqual(sanitiseHref('http://evil.example/x'), null);
});

test('sanitiseHref rejects a javascript: URL', () => {
    assert.strictEqual(sanitiseHref('javascript:alert(1)'), null);
});

test('sanitiseHref rejects a javascript: URL with leading whitespace', () => {
    // The URL constructor trims leading C0/space, so the trim-then-parse bypass that
    // defeats a string-prefix check does not defeat this one.
    assert.strictEqual(sanitiseHref('  javascript:alert(1)'), null);
    assert.strictEqual(sanitiseHref('\tjavascript:alert(1)'), null);
});

test('sanitiseHref rejects a data: URL', () => {
    assert.strictEqual(sanitiseHref('data:text/html,<script>alert(1)</script>'), null);
});

test('sanitiseHref rejects a falsy url without parsing', () => {
    assert.strictEqual(sanitiseHref(''), null);
    assert.strictEqual(sanitiseHref(null), null);
    assert.strictEqual(sanitiseHref(undefined), null);
});

test('sanitiseHref rejects an unparseable url', () => {
    // No base can rescue this, so the URL constructor throws and the catch returns null.
    assert.strictEqual(sanitiseHref('http://['), null);
});

test('sanitiseHref: a protocol-relative URL inherits the page scheme', () => {
    // '//host/x' resolves against the origin, so under https it is an allowed external
    // https link. Documented here because the same input behaves differently on the Pi
    // (see the plain-http test below) — that asymmetry is the function working as designed,
    // not a special case.
    assert.strictEqual(sanitiseHref('//world.openfoodfacts.org/x'), '//world.openfoodfacts.org/x');
});

test('sanitiseHref allows same-origin over plain http (the Pi deployment)', () => {
    // The whole reason the same-origin branch skips the https check: the Pi serves over
    // plain HTTP, so an absolute self-link and a relative link must both survive, while an
    // external http link must still be rejected under that same origin.
    const original = globalThis.window.location.origin;
    globalThis.window.location.origin = 'http://pi.local';
    try {
        assert.strictEqual(
            sanitiseHref('http://pi.local/product.html?barcode=5014788110140&retailer_id=1'),
            'http://pi.local/product.html?barcode=5014788110140&retailer_id=1',
        );
        assert.strictEqual(sanitiseHref('/inventory.html'), '/inventory.html');
        assert.strictEqual(sanitiseHref('http://evil.example/x'), null);
        // Under a plain-http origin the protocol-relative form downgrades and is rejected.
        assert.strictEqual(sanitiseHref('//evil.example/x'), null);
    } finally {
        globalThis.window.location.origin = original;
    }
});

// ── esc ─────────────────────────────────────────────────────────────────────

test('esc escapes all five HTML-significant characters', () => {
    assert.strictEqual(
        esc(`&<>"'`),
        '&amp;&lt;&gt;&quot;&#39;',
    );
});

test('esc neutralises an injected tag', () => {
    assert.strictEqual(
        esc('<script>alert("x")</script>'),
        '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;',
    );
});

test('esc maps null and undefined to the empty string', () => {
    assert.strictEqual(esc(null), '');
    assert.strictEqual(esc(undefined), '');
});

test('esc coerces non-strings, and 0 is NOT treated as absent', () => {
    // `?? ''` only catches null/undefined, so a zero quantity still renders as "0".
    assert.strictEqual(esc(0), '0');
    assert.strictEqual(esc(415), '415');
    assert.strictEqual(esc(false), 'false');
});

test('esc leaves already-safe text untouched', () => {
    assert.strictEqual(esc('Baked Beans 415g'), 'Baked Beans 415g');
});

// ── rowKey ──────────────────────────────────────────────────────────────────

test('rowKey is stable: same (barcode, retailer) → same key', () => {
    assert.strictEqual(
        rowKey('5014788110140', 1),
        rowKey('5014788110140', 1),
    );
});

test('rowKey distinguishes retailer: same barcode, different retailer → different keys', () => {
    assert.notStrictEqual(
        rowKey('5014788110140', 1),
        rowKey('5014788110140', 2),
    );
});

test('rowKey distinguishes barcode: same retailer, different barcode → different keys', () => {
    assert.notStrictEqual(
        rowKey('5014788110140', 1),
        rowKey('5000112548167', 1),
    );
});

test('rowKey separator is collision-free: ("1","23") and ("12","3") do not collide', () => {
    assert.notStrictEqual(
        rowKey('1', '23'),
        rowKey('12', '3'),
    );
});

test('rowKey coerces retailer: numeric 1 and string "1" produce the same key', () => {
    // Load-bearing: the WebSocket delivers retailer as a number, but data-* attributes
    // read back as strings — the String() coercion is what makes the two match.
    assert.strictEqual(
        rowKey('5014788110140', 1),
        rowKey('5014788110140', '1'),
    );
});
