// SPIKE A — REMOVE BEFORE PHASE 2
function log(msg) {
  const ts = new Date().toISOString().slice(11, 23);
  const el = document.createElement('div');
  el.className = 'entry';
  el.innerHTML = `<span class="ts">[${ts}]</span> ${msg}`;
  document.getElementById('log').appendChild(el);
  document.getElementById('log').scrollTop = 9999;
  console.log(ts, msg);
}

let reconnectDelay = 1000;

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => {
    reconnectDelay = 1000;
    log('Connected');
    ws.send(JSON.stringify({ type: 'ping' }));
  };

  ws.onmessage = (event) => {
    log('Message: ' + event.data);
  };

  ws.onerror = (event) => {
    log('Error: ' + (event.message || 'WebSocket error'));
  };

  ws.onclose = () => {
    log(`Disconnected. Reconnecting in ${reconnectDelay}ms…`);
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  };
}

connect();
