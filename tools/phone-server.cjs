'use strict';
const http = require('node:http');
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const PROJECT = path.resolve(__dirname, '..');
const COOKIE = 'image_ranker_phone';
const SESSION_MS = 24 * 60 * 60 * 1000;
const cookieValue = (token, maxAge) => `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${maxAge}`;

function ipNumber(ip) {
  if (net.isIP(ip) !== 4) throw new Error('Invalid IPv4 bind/subnet.');
  return ip.split('.').reduce((n, octet) => n * 256 + Number(octet), 0) >>> 0;
}
function privateBlock(ip) {
  const n = ipNumber(ip);
  if ((n >>> 24) === 10) return '10/8';
  if ((n >>> 20) === 2753) return '172.16/12';
  if ((n >>> 16) === 49320) return '192.168/16';
  if ((n >>> 24) === 127) return '127/8';
  return null;
}
function admission(bind, subnet) {
  if (!privateBlock(bind)) throw new Error('bind must be a private or loopback IPv4 address.');
  if (typeof subnet !== 'string' || !/^\d+\.\d+\.\d+\.\d+\/\d+$/.test(subnet)) throw new Error('Explicit IPv4 subnet required.');
  const [network, rawBits] = subnet.split('/');
  const bits = Number(rawBits), n = ipNumber(network);
  if (bits < 1 || bits > 32) throw new Error('Invalid subnet prefix.');
  const mask = (0xffffffff << (32 - bits)) >>> 0;
  const end = (n | (~mask >>> 0)) >>> 0;
  if ((n & mask) >>> 0 !== n || privateBlock(network) !== privateBlock(bind) ||
      privateBlock(network) !== privateBlock(String(end >>> 24) + '.' + ((end >>> 16) & 255) + '.' + ((end >>> 8) & 255) + '.' + (end & 255))) {
    throw new Error('subnet must be a canonical private network.');
  }
  const contains = ip => { try { return ((ipNumber(ip) & mask) >>> 0) === n; } catch { return false; } };
  if (!contains(bind)) throw new Error('subnet must contain bind address.');
  return contains;
}

const LOGIN = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Pair Image Ranker</title><link rel="stylesheet" href="/pairing.css"></head><body><main><h1>Pair this phone</h1><p>Enter the one-time code shown on the PC. Trusted home Wi-Fi only: HTTP is not encrypted.</p><form method="post" action="/login"><label>Pairing code <input name="code" inputmode="numeric" pattern="[0-9]{8}" maxlength="8" autocomplete="off" required></label><button>Pair</button></form></main></body></html>';
const LOGIN_CSS = '*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:#111824;color:#eef3f8;font-family:Segoe UI,Arial,sans-serif}body{padding:24px 16px}main{max-width:420px;margin:8vh auto}h1{font-size:28px}p{font-size:15px;line-height:1.55;color:#b6c2d1}label{display:block;margin-top:24px;font-size:15px}input,button{display:block;width:100%;min-height:52px;margin-top:10px;border-radius:8px;font:inherit}input{background:#172334;color:#eef3f8;border:1px solid #52647b;padding:10px 12px;font-size:24px;letter-spacing:.15em}button{margin-top:18px;padding:12px;background:#74d6e5;color:#101824;border:0;font-weight:600;font-size:17px;touch-action:manipulation}:focus-visible{outline:3px solid #74d6e5;outline-offset:3px}';
function securityHeaders(res, nonce) {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Content-Security-Policy', `default-src 'self'; script-src 'self'${nonce ? ` 'nonce-${nonce}'` : ''}; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'`);
}
function json(res, status, data) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(data));
}
function inputError(status, message) { return Object.assign(new Error(message), { status }); }
function readBody(req, limit = 4096, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    if (Number(req.headers['content-length']) > limit) {
      req.pause(); reject(inputError(413, 'Request body too large.')); return;
    }
    const chunks = []; let size = 0;
    const cleanup = () => { clearTimeout(timer); req.off('data', data); req.off('end', end); req.off('error', error); req.off('aborted', aborted); };
    const timer = setTimeout(() => { cleanup(); req.pause(); reject(inputError(408, 'Request body deadline exceeded.')); }, timeoutMs);
    const error = () => { cleanup(); reject(inputError(400, 'Incomplete request body.')); };
    const aborted = error;
    const data = chunk => {
      size += chunk.length;
      if (size > limit) { cleanup(); req.pause(); reject(inputError(413, 'Request body too large.')); }
      else chunks.push(chunk);
    };
    const end = () => { cleanup(); resolve(Buffer.concat(chunks)); };
    req.on('data', data); req.on('end', end); req.on('error', error); req.on('aborted', aborted);
  });
}
function rewriteMediaURLs(data, config) {
  const stack = [data];
  while (stack.length) {
    const node = stack.pop();
    if (!node || typeof node !== 'object') continue;
    for (const [key, value] of Object.entries(node)) {
      if ((key === 'url' || key === 'poster_url') && value !== null) {
        if (typeof value !== 'string') throw new Error('Invalid media URL.');
        const base = `http://127.0.0.1:${config.port}`;
        const url = new URL(value, base);
        if (url.origin !== base || url.username || url.password || !/^\/api\/(image|video)\/[0-9]+$/.test(url.pathname)) throw new Error('Invalid media URL.');
        node[key] = url.pathname;
      } else if (value && typeof value === 'object') stack.push(value);
    }
  }
  const body = JSON.stringify(data);
  if (body.includes(config.api_token)) throw new Error('Private content.');
  return body;
}
function coreConfig(options) {
  let config = options.config;
  if (config === undefined) {
    try { config = JSON.parse(fs.readFileSync(options.configPath || path.join(PROJECT, 'config.json'), 'utf8')); }
    catch { throw new Error('Cannot read valid private core configuration.'); }
  }
  if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error('Invalid core configuration.');
  const { api_token: token, port, host = '127.0.0.1' } = config;
  if (host !== '127.0.0.1') throw new Error('Core host must remain loopback 127.0.0.1.');
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid core port configuration.');
  if (typeof token !== 'string' || !/^[A-Za-z0-9_-]{32,256}$/.test(token) || new Set(token).size < 8) throw new Error('Invalid private api_token configuration.');
  return Object.freeze({ port, api_token: token });
}
function createPhoneServer(options = {}) {
  options = { ...options, port: options.port ?? 47832 };
  const config = coreConfig(options);
  const deadlines = {};
  for (const [key, fallback] of Object.entries({ headersTimeoutMs: 5000, bodyTimeoutMs: 5000, upstreamTimeoutMs: 10000 })) {
    const value = options[key] ?? fallback;
    if (!Number.isInteger(value) || value < 25 || value > 30000) throw new Error('Invalid deadline configuration.');
    deadlines[key] = value;
  }
  const contains = admission(options.bind, options.subnet);
  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535) throw new Error('Invalid phone port.');
  const stateFile = path.resolve(options.stateFile || path.join(PROJECT, 'data/phone-access.json'));
  const widgetDir = path.resolve(options.widgetDir || path.join(PROJECT, 'widget'));
  const assets = new Map([
    ['/', ['index.html', 'text/html; charset=utf-8']], ['/index.html', ['index.html', 'text/html; charset=utf-8']],
    ['/runtime.js', ['runtime.js', 'text/javascript; charset=utf-8']], ['/style.css', ['style.css', 'text/css; charset=utf-8']],
    ['/phone.css', ['phone.css', 'text/css; charset=utf-8']], ['/translation.json', ['translation.json', 'application/json; charset=utf-8']],
    ['/resources/icon.svg', ['resources/icon.svg', 'image/svg+xml']],
  ]);
  const sessions = new Map();
  const now = options.now || Date.now;
  let failures = [];
  function rateBlocked(ip) {
    failures = failures.filter(f => now() - f.at < 300000);
    return failures.length >= 30 || failures.filter(f => f.ip === ip).length >= 5;
  }
  let pairingCode = '';
  const issuedCodes = new Set();
  function rotateCode() {
    if (issuedCodes.size >= 100000) throw new Error('Restart required to issue more pairing codes.');
    let next, attempts = 0;
    do {
      if (++attempts > 128) throw new Error('Pairing entropy unavailable.');
      next = String(crypto.randomInt(0, 100000000)).padStart(8, '0');
    } while (issuedCodes.has(next));
    const owner = { url: `http://${options.bind}:${server.address().port}/`, pairing_code: next, pid: process.pid };
    fs.mkdirSync(path.dirname(stateFile), { recursive: true });
    const temp = `${stateFile}.${crypto.randomBytes(8).toString('hex')}.tmp`;
    try {
      fs.writeFileSync(temp, JSON.stringify(owner, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
      fs.renameSync(temp, stateFile);
    } finally { fs.rmSync(temp, { force: true }); }
    pairingCode = next;
    issuedCodes.add(next);
    server.emit('pairing', Object.freeze(owner));
  }
  async function proxyJSON(req, res, route, body) {
    let upstream, timer;
    const abort = () => upstream?.destroy(new Error('Phone disconnected.'));
    res.once('close', abort);
    try {
      const response = await new Promise((resolve, reject) => {
        upstream = http.request({ hostname: '127.0.0.1', port: config.port, path: route, method: req.method,
          agent: false, headers: { Authorization: `Bearer ${config.api_token}`,
            ...(body ? { 'Content-Type': 'application/json', 'Content-Length': body.length } : {}) } }, resolve);
        upstream.on('error', reject);
        timer = setTimeout(() => upstream.destroy(new Error('Private core deadline exceeded.')), deadlines.upstreamTimeoutMs);
        upstream.end(body);
      });
      if (response.statusCode < 200 || (response.statusCode >= 300 && response.statusCode < 400)) {
        response.destroy(); throw new Error('Unexpected upstream status.');
      }
      const chunks = []; let size = 0;
      for await (const chunk of response) {
        size += chunk.length;
        if (size > 1024 * 1024) { response.destroy(); throw new Error('JSON response too large.'); }
        chunks.push(chunk);
      }
      const rewritten = rewriteMediaURLs(JSON.parse(Buffer.concat(chunks).toString('utf8')), config);
      res.writeHead(response.statusCode, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(rewritten);
    } catch {
      upstream?.destroy();
      if (!res.destroyed) json(res, 502, { error: 'Private core unavailable or unsafe response.' });
    } finally { clearTimeout(timer); res.off('close', abort); }
  }
  function proxyMedia(req, res, route) {
    const headers = { Authorization: `Bearer ${config.api_token}` };
    if (req.headers.range !== undefined) {
      if (req.headers.range.length > 100 || !/^bytes=(?:[0-9]+-[0-9]*|-[0-9]+)$/.test(req.headers.range)) throw inputError(400, 'Expected one byte Range.');
      headers.Range = req.headers.range;
    }
    let headerTimer;
    const upstream = http.request({ hostname: '127.0.0.1', port: config.port, path: route,
      method: req.method, agent: false, headers }, response => {
      clearTimeout(headerTimer);
      if (res.destroyed) { response.destroy(); return; }
      if (response.statusCode >= 400 && response.statusCode <= 599) {
        if (response.statusCode === 416 && /^bytes \*\/[0-9]+$/.test(response.headers['content-range'] || '')) res.setHeader('Content-Range', response.headers['content-range']);
        response.destroy(); json(res, response.statusCode, { error: 'Media unavailable.' }); return;
      }
      const mime = (response.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
      const safe = route.startsWith('/api/video/') ? ['video/mp4', 'video/webm'] :
        ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/avif', 'image/bmp', 'image/tiff'];
      const validRange = /^bytes [0-9]+-[0-9]+\/[0-9]+$/.test(response.headers['content-range'] || '');
      if (![200, 206].includes(response.statusCode) || !safe.includes(mime) ||
          (response.headers['content-encoding'] && response.headers['content-encoding'] !== 'identity') ||
          (response.statusCode === 206 && (!headers.Range || !validRange))) {
        response.destroy(); json(res, 502, { error: 'Unsafe media response.' }); return;
      }
      const output = { 'Content-Type': mime };
      if (/^[0-9]+$/.test(response.headers['content-length'] || '')) output['Content-Length'] = response.headers['content-length'];
      if (response.headers['accept-ranges'] === 'bytes') output['Accept-Ranges'] = 'bytes';
      if (/^bytes [0-9]+-[0-9]+\/[0-9]+$/.test(response.headers['content-range'] || '')) output['Content-Range'] = response.headers['content-range'];
      res.writeHead(response.statusCode, output);
      response.on('error', () => res.destroy());
      if (req.method === 'HEAD') { response.resume(); res.end(); }
      else response.pipe(res);
    });
    upstream.on('error', () => {
      clearTimeout(headerTimer);
      if (res.destroyed || res.writableEnded) return;
      if (res.headersSent) res.destroy();
      else json(res, 502, { error: 'Private core media unavailable.' });
    });
    headerTimer = setTimeout(() => upstream.destroy(new Error('Media headers deadline exceeded.')), deadlines.upstreamTimeoutMs);
    upstream.setTimeout(15000, () => upstream.destroy(new Error('Media idle deadline exceeded.')));
    res.once('close', () => { clearTimeout(headerTimer); upstream.destroy(); });
    upstream.end();
  }
  const handle = async (req, res) => {
    securityHeaders(res);
    const hostCount = req.rawHeaders.filter((value, index) => index % 2 === 0 && value.toLowerCase() === 'host').length;
    if (hostCount !== 1 || !contains(req.socket.remoteAddress) || req.headers.host !== `${options.bind}:${server.address().port}`) {
      json(res, 403, { error: 'Forbidden.' }); return;
    }
    if (req.method === 'POST') {
      const origin = `http://${options.bind}:${server.address().port}`;
      const site = req.headers['sec-fetch-site'];
      if (site === 'cross-site' || (req.headers.origin !== undefined && req.headers.origin !== origin) ||
          (req.headers.origin === undefined && site !== undefined)) {
        json(res, 403, { error: 'Forbidden origin.' }); return;
      }
    }
    const token = (req.headers.cookie || '').split(';').map(v => v.trim()).find(v => v.startsWith(`${COOKIE}=`))?.slice(COOKIE.length + 1);
    for (const [id, stored] of sessions) if (stored.expires <= now()) sessions.delete(id);
    const session = sessions.get(token);
    const authenticated = session && session.ip === req.socket.remoteAddress;
    if (req.method === 'POST' && req.url === '/login') {
      if (!/^application\/x-www-form-urlencoded(?:\s*;|$)/i.test(req.headers['content-type'] || '')) throw inputError(415, 'Expected a pairing form.');
      const body = await readBody(req, 4096, deadlines.bodyTimeoutMs);
      const form = new URLSearchParams(body.toString());
      const code = form.size === 1 ? form.get('code') || '' : '';
      if (rateBlocked(req.socket.remoteAddress)) {
        res.setHeader('Retry-After', '300'); json(res, 429, { error: 'Pairing temporarily rate limited.' }); return;
      }
      if (!/^\d{8}$/.test(code) || !crypto.timingSafeEqual(Buffer.from(code), Buffer.from(pairingCode))) {
        failures.push({ ip: req.socket.remoteAddress, at: now() });
        json(res, 401, { error: 'Invalid pairing code.' }); return;
      }
      rotateCode();
      const id = crypto.randomBytes(32).toString('hex');
      if (sessions.size >= 16) sessions.delete(sessions.keys().next().value);
      sessions.set(id, { ip: req.socket.remoteAddress, expires: now() + SESSION_MS });
      res.writeHead(303, { Location: '/', 'Set-Cookie': cookieValue(id, 86400) }); res.end(); return;
    }
    if (req.method === 'POST' && req.url === '/logout' && authenticated) {
      sessions.delete(token);
      res.writeHead(303, { Location: '/', 'Set-Cookie': cookieValue('', 0) }); res.end(); return;
    }
    const pathname = req.url.split('?')[0];
    if (req.method === 'GET' && req.url === '/pairing.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' }); res.end(LOGIN_CSS); return;
    }
    if (!authenticated) {
      if (req.method === 'GET' && pathname === '/') {
        // Native form navigations under no-referrer serialize Origin as null.
        // Keep same-origin form CSRF checks strict without leaking foreign referrers.
        res.setHeader('Referrer-Policy', 'same-origin');
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(LOGIN);
      } else json(res, 401, { error: 'Authentication required.' });
      return;
    }
    if ((req.method === 'GET' || req.method === 'HEAD') && /^\/api\/(image|video)\/[0-9]+$/.test(pathname)) {
      const query = new URLSearchParams(req.url.split('?').slice(1).join('?')); query.delete('token');
      if (query.size) throw inputError(400, 'Invalid API query.');
      proxyMedia(req, res, pathname); return;
    }
    if (req.method === 'POST' && (pathname === '/api/vote' || pathname === '/api/skip')) {
      const query = new URLSearchParams(req.url.split('?').slice(1).join('?')); query.delete('token');
      if (query.size) throw inputError(400, 'Invalid API query.');
      if (!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] || '')) throw inputError(415, 'Expected JSON.');
      const body = await readBody(req, 4096, deadlines.bodyTimeoutMs);
      let payload;
      try { payload = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(body)); }
      catch { throw inputError(400, 'Invalid JSON.'); }
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw inputError(400, 'Expected a JSON object.');
      await proxyJSON(req, res, pathname, body); return;
    }
    if (req.method === 'GET' && (pathname === '/api/state' || pathname === '/api/health')) {
      const query = new URLSearchParams(req.url.split('?').slice(1).join('?'));
      query.delete('token');
      if ([...query.keys()].some(key => key !== 'pair_id') || query.getAll('pair_id').length > 1 ||
          (query.has('pair_id') && (!query.get('pair_id') || pathname !== '/api/state'))) {
        json(res, 400, { error: 'Invalid API query.' }); return;
      }
      const route = pathname + (query.size ? `?${query}` : '');
      await proxyJSON(req, res, route); return;
    }
    if (req.method === 'GET' && pathname === '/defaults.js') {
      res.writeHead(200, { 'Content-Type': 'text/javascript; charset=utf-8' });
      res.end("window.ImageRankerPhoneMode=true;window.ImageRankerDefaults={endpoint:location.origin,apiToken:''};\n"); return;
    }
    if (req.method === 'GET' && assets.has(pathname)) {
      const [file, type] = assets.get(pathname);
      try {
        let body = fs.readFileSync(path.join(widgetDir, file), 'utf8');
        if (body.includes(config.api_token)) throw new Error('Private content.');
        if (file === 'index.html') {
          const nonce = crypto.randomBytes(18).toString('base64');
          body = body.replace('</head>', '<link rel="stylesheet" href="phone.css"></head>')
            .replace('</main>', '<form class="phone-session" method="post" action="/logout"><button type="submit">Disconnect phone</button></form></main>')
            .replace(/<script(?![^>]*\bsrc=)([^>]*)>/gi, `<script nonce="${nonce}"$1>`);
          securityHeaders(res, nonce);
          res.setHeader('Referrer-Policy', 'same-origin');
        }
        res.writeHead(200, { 'Content-Type': type }); res.end(body);
      } catch { json(res, 502, { error: 'Public widget asset unavailable.' }); }
      return;
    }
    json(res, 404, { error: 'Not found.' });
  };
  const server = http.createServer({ maxHeaderSize: 8192, headersTimeout: deadlines.headersTimeoutMs,
    requestTimeout: deadlines.headersTimeoutMs + deadlines.bodyTimeoutMs,
    connectionsCheckingInterval: Math.min(1000, deadlines.headersTimeoutMs) }, (req, res) => {
    req.on('error', () => {});
    handle(req, res).catch(error => {
      if (res.destroyed) return;
      if (res.headersSent) { res.destroy(); return; }
      res.setHeader('Connection', 'close');
      json(res, error.status || 502, { error: error.status ? error.message : 'Phone gateway unavailable.' });
    });
  });
  server.on('clientError', (error, socket) => {
    if (socket.destroyed || !socket.writable) return;
    const status = error.code === 'ERR_HTTP_REQUEST_TIMEOUT' ? 408 : error.code === 'HPE_HEADER_OVERFLOW' ? 431 : 400;
    const headers = [];
    securityHeaders({ setHeader: (key, value) => headers.push(`${key}: ${value}`) });
    socket.end(`HTTP/1.1 ${status} ${http.STATUS_CODES[status]}\r\n${headers.join('\r\n')}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n`);
  });
  server.maxConnections = 32;
  server.maxRequestsPerSocket = 100;
  server.maxHeadersCount = 32;
  server.keepAliveTimeout = 2000;
  server.setTimeout(15000, socket => socket.destroy());
  server.on('listening', () => {
    try { rotateCode(); }
    catch {
      server.close();
      server.emit('error', new Error('Cannot publish private phone access owner file.'));
    }
  });
  server.on('close', () => {
    sessions.clear();
    try {
      const owner = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
      if (owner.pid === process.pid && owner.pairing_code === pairingCode) fs.unlinkSync(stateFile);
    } catch { /* Never remove another owner's file or fail shutdown. */ }
  });
  const listen = server.listen.bind(server);
  server.listen = callback => listen(options.port, options.bind, callback);
  server.start = () => new Promise((resolve, reject) => {
    if (server.listening) { resolve(server); return; }
    const ready = () => { server.off('error', failed); resolve(server); };
    const failed = error => { server.off('listening', ready); reject(error); };
    server.once('listening', ready); server.once('error', failed);
    try { server.listen(); } catch (error) { server.off('error', failed); failed(error); }
  });
  server.stop = () => new Promise((resolve, reject) => {
    server.close(error => error && error.code !== 'ERR_SERVER_NOT_RUNNING' ? reject(error) : resolve());
    server.closeAllConnections();
  });
  return server;
}
module.exports = { createPhoneServer };

const USAGE = 'Usage: node tools/phone-server.cjs --bind <privateIPv4> --subnet <actual CIDR> [--port 47832] [--config config.json] [--state-file PATH]';
async function main(argv) {
  if (argv.length === 1 && argv[0] === '--help') { console.log(USAGE); return; }
  const flags = new Map([['--bind', 'bind'], ['--subnet', 'subnet'], ['--port', 'port'],
    ['--config', 'configPath'], ['--state-file', 'stateFile']]);
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = flags.get(argv[i]);
    if (!key || Object.hasOwn(options, key) || !argv[i + 1] || argv[i + 1].startsWith('--')) throw new Error('Invalid or duplicate command-line option. ' + USAGE);
    options[key] = argv[i + 1];
  }
  if (!options.bind || !options.subnet) throw new Error('Explicit --bind and --subnet are required. ' + USAGE);
  if (options.port !== undefined) {
    if (!/^[0-9]{1,5}$/.test(options.port)) throw new Error('Invalid phone port.');
    options.port = Number(options.port);
  }
  // Validate opt-in network scope before opening even the private configuration.
  admission(options.bind, options.subnet);
  const server = createPhoneServer(options);
  server.on('pairing', owner => console.log(`Phone URL: ${owner.url}\nPairing code: ${owner.pairing_code}`));
  console.log('Trusted home Wi-Fi only. HTTP is not encrypted; do not use guest/shared/public Wi-Fi or router forwarding.');
  await server.start();
  const shutdown = () => server.stop().catch(() => { process.exitCode = 1; });
  process.once('SIGINT', shutdown); process.once('SIGTERM', shutdown);
  server.on('error', () => { console.error('Phone gateway listener failed.'); process.exitCode = 1; shutdown(); });
}
if (require.main === module) main(process.argv.slice(2)).catch(error => {
  console.error('Phone gateway: ' + error.message); process.exitCode = 1;
});

