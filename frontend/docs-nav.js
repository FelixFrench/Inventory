async function openDocs() {
    try {
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
    } catch (_) {
        alert('Could not open docs: check connection');
    }
}

document.getElementById('docs-link').addEventListener('click', function(e) {
    e.preventDefault();
    openDocs();
});
