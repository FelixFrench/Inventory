const banner = document.getElementById('banner');
const label = document.getElementById('mode-label');
const sub = document.getElementById('mode-sub');
const btn = document.getElementById('toggle-btn');
const loading = document.getElementById('loading');
const errorMsg = document.getElementById('error-msg');

let currentMode = null;

function applyMode(mode) {
  currentMode = mode;
  if (mode === 'in') {
    banner.className = 'mode-banner mode-in';
    label.textContent = 'Scanning IN';
    sub.textContent = 'Items are being added to inventory';
    btn.textContent = 'Switch to OUT';
    btn.className = 'toggle-btn to-out';
  } else {
    banner.className = 'mode-banner mode-out';
    label.textContent = 'Scanning OUT';
    sub.textContent = 'Items are being removed from inventory';
    btn.textContent = 'Switch to IN';
    btn.className = 'toggle-btn to-in';
  }
  loading.style.display = 'none';
  banner.style.display = '';
  btn.style.display = '';
}

function showError(msg) {
  errorMsg.textContent = msg;
  errorMsg.style.display = '';
}

async function toggleMode() {
  const next = currentMode === 'in' ? 'out' : 'in';
  btn.disabled = true;
  try {
    const resp = await fetch('/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
      body: JSON.stringify({ mode: next }),
    });
    if (!resp.ok) throw new Error('Server error ' + resp.status);
    const data = await resp.json();
    applyMode(data.mode);
    errorMsg.style.display = 'none';
  } catch (e) {
    showError('Failed to switch mode. Check connection and try again.');
  } finally {
    btn.disabled = false;
  }
}

(async function init() {
  try {
    const resp = await fetch('/mode', { headers: { 'X-API-Key': API_KEY } });
    if (!resp.ok) throw new Error('Server error ' + resp.status);
    const data = await resp.json();
    applyMode(data.mode);
  } catch (e) {
    loading.style.display = 'none';
    showError('Could not load current mode. Check connection.');
  }
})();

btn.addEventListener('click', toggleMode);
