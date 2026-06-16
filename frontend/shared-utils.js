// Shared frontend utilities, loaded on every page before the page-specific
// script (after config.js and docs-nav.js).

// HTML-escape a value for safe interpolation into innerHTML. Escapes the five
// characters that matter in HTML/attribute contexts, including the single quote.
function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
