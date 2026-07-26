// Minimal obs-websocket v5 client (Node 22+ built-in WebSocket, no auth).
export function connect(url = 'ws://127.0.0.1:4455') {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    const pending = new Map();
    let seq = 0;

    ws.addEventListener('message', ev => {
      const msg = JSON.parse(ev.data);
      if (msg.op === 0) {
        // Hello -> Identify. Auth is expected to be disabled on this box.
        if (msg.d.authentication) {
          reject(new Error('OBS requires authentication; disable it in WebSocket Server Settings'));
          ws.close();
          return;
        }
        ws.send(JSON.stringify({ op: 1, d: { rpcVersion: 1, eventSubscriptions: 0 } }));
      } else if (msg.op === 2) {
        resolve(api);
      } else if (msg.op === 7) {
        const p = pending.get(msg.d.requestId);
        if (!p) return;
        pending.delete(msg.d.requestId);
        if (msg.d.requestStatus.result) p.resolve(msg.d.responseData || {});
        else p.reject(new Error(`${msg.d.requestType}: ${msg.d.requestStatus.code} ${msg.d.requestStatus.comment || ''}`));
      }
    });
    ws.addEventListener('error', () => reject(new Error('WebSocket error — is OBS running with the server enabled?')));

    const api = {
      call(requestType, requestData = {}) {
        const requestId = 'r' + (++seq);
        return new Promise((res, rej) => {
          pending.set(requestId, { resolve: res, reject: rej });
          ws.send(JSON.stringify({ op: 6, d: { requestType, requestId, requestData } }));
        });
      },
      close: () => ws.close()
    };
  });
}

export const sleep = ms => new Promise(r => setTimeout(r, ms));
