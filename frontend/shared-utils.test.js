// Unit tests for shared-utils.js, run with the Node built-in test runner:
//   node --test frontend/
// No package.json / npm dependencies — shared-utils.js exports its functions via a
// guarded `module.exports` that the browser ignores.

const test = require('node:test');
const assert = require('node:assert');

// sanitiseHref() resolves relative URLs against window.location.origin, so the
// module needs a window global before it is required. Point it at a fixed origin.
globalThis.window = { location: { origin: 'https://app.example' } };

const { sanitiseHref } = require('./shared-utils.js');

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
