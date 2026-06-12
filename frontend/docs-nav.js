async function openDocs() {
    const res = await fetch('/docs-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: API_KEY }),
    });
    if (res.ok) {
        window.location.href = '/docs';
    } else {
        alert('Could not open docs: check API key in config.js');
    }
}
