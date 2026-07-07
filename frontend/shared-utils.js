// Shared frontend utilities, loaded on every page before the page-specific
// script (after config.js and docs-nav.js).

// HTML-escape a value for safe interpolation into innerHTML. Escapes the five
// characters that matter in HTML/attribute contexts, including the single quote.
function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Validate a URL for use in an anchor href. Returns the URL only if it is safe,
// otherwise null. esc() does NOT cover this — it escapes HTML entities, not URL
// schemes, so it can't stop javascript:/data: scheme injection from a value that
// originates outside our control (e.g. a price_url from an external retailer API,
// or a server-built internal page link).
//
// Parse with the URL constructor (never string prefix/regex checks, which are a
// known bypass vector) resolving relative paths against the current origin:
//   - same-origin results are allowed regardless of http:/https: (the site itself
//     currently runs over plain HTTP on the Pi, and internal links are relative);
//   - cross-origin results keep the https-only policy (external OFF/Sainsbury's links);
//   - anything that fails to parse, or is cross-origin non-https (e.g. javascript:,
//     data:, external http:), is rejected.
function sanitiseHref(url) {
    if (!url) return null;
    try {
        const parsed = new URL(url, window.location.origin);
        if (parsed.origin === window.location.origin) return url;
        return parsed.protocol === 'https:' ? url : null;
    } catch (_) {
        return null;
    }
}

// Toast notifications. Creates a transient .toast element (styled in base.css:
// .toast plus .toast--ok / .toast--error / .toast--info) and auto-removes it after
// 4s, replacing any toast still on screen. `type` defaults to the neutral 'info'
// variant so single-argument callers still get a styled, positioned toast.
let _activeToast = null;

function showToast(message, type = 'info') {
    if (_activeToast) {
        _activeToast.remove();
        _activeToast = null;
    }
    const toast = document.createElement('div');
    toast.className = `toast toast--${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    _activeToast = toast;
    setTimeout(() => {
        toast.remove();
        if (_activeToast === toast) _activeToast = null;
    }, 4000);
}

// Exported for the Node test runner (`node --test frontend/`); ignored in the
// browser, where `module` is undefined and these are plain globals.
if (typeof module !== 'undefined') {
    module.exports = { esc, sanitiseHref, showToast };
}
