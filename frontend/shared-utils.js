// Shared frontend utilities, loaded on every page before the page-specific
// script (after config.js and docs-nav.js).

// HTML-escape a value for safe interpolation into innerHTML. Escapes the five
// characters that matter in HTML/attribute contexts, including the single quote.
function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Validate a URL for use in an anchor href. Returns the URL only if it parses
// and uses the https: scheme, otherwise null. Guards against javascript:/data:
// scheme injection from any URL value that originates outside our control
// (e.g. a price_url from an external retailer API). esc() does NOT cover this —
// it escapes HTML entities, not URL schemes.
function sanitiseHref(url) {
    if (!url) return null;
    try {
        return new URL(url).protocol === 'https:' ? url : null;
    } catch (_) {
        return null;
    }
}
