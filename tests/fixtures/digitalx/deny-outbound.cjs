// Sandbox is a local server only. Even accidental paper/live mode cannot dial out.
const net = require('node:net');
const tls = require('node:tls');
const deny = () => { throw new Error('Digital X sandbox outbound network denied'); };
net.Socket.prototype.connect = deny;
tls.connect = deny;
globalThis.fetch = deny;
globalThis.WebSocket = class { constructor() { deny(); } };
