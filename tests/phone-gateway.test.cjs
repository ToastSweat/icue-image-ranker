'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { once } = require('node:events');
const SCRATCH = 'C:/Users/pada1/AppData/Local/hermes/profiles/chatgpt/cache/scratch';
const SECRET = 'fixture_CORE_secret_abcdef0123456789_not_live';
function gatewayModule() {
  try { return require('../tools/phone-server.cjs'); }
  catch (error) { if (error.code === 'MODULE_NOT_FOUND') return {}; throw error; }
}
async function fixture(t, options = {}) {
  const { createPhoneServer } = gatewayModule();
  assert.equal(typeof createPhoneServer, 'function', 'export createPhoneServer');
  const dir = fs.mkdtempSync(path.join(SCRATCH, 'phone-gateway-'));
  const requests = [];
  const upstream = http.createServer((req, res) => {
    requests.push({ method: req.method, url: req.url, headers: req.headers });
    if (options.upstream) return options.upstream(req, res);
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ ok: true }));
  });
  t.after(() => new Promise(resolve => { upstream.close(resolve); upstream.closeAllConnections(); }));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  upstream.listen(0, '127.0.0.1'); await once(upstream, 'listening');
  const widgetDir = path.join(dir, 'widget'); fs.mkdirSync(widgetDir);
  for (const [name, content] of Object.entries({
    'index.html': '<!doctype html><html><head></head><body><main><p>Fixture public widget</p></main><script src="defaults.js"></script><script src="runtime.js"></script><script>window.fixtureBoot=true;</script></body></html>',
    'runtime.js': 'window.fixtureRuntime=true;', 'style.css': 'body{color:white}',
    'phone.css': 'body{touch-action:manipulation}', 'translation.json': '{"en":{}}',
    'defaults.js': `window.PRIVATE=${JSON.stringify(SECRET)};`,
  })) fs.writeFileSync(path.join(widgetDir, name), content);
  fs.mkdirSync(path.join(widgetDir, 'resources'));
  fs.writeFileSync(path.join(widgetDir, 'resources/icon.svg'), '<svg xmlns="http://www.w3.org/2000/svg"/>');
  const config = { host: '127.0.0.1', port: upstream.address().port, api_token: SECRET };
  const stateFile = path.join(dir, 'phone-access.json');
  const gateway = createPhoneServer({ bind: '127.0.0.1', subnet: '127.0.0.0/8', port: 0,
    config, widgetDir, stateFile, ...options.gateway });
  t.after(() => new Promise(resolve => { gateway.close(resolve); gateway.closeAllConnections(); }));
  gateway.listen(); await once(gateway, 'listening');
  const port = gateway.address().port;
  function request(route = '/', { method = 'GET', headers = {}, body, localAddress } = {}) {
    return new Promise((resolve, reject) => {
      const req = http.request({ hostname: '127.0.0.1', port, path: route, method, localAddress,
        agent: false, headers: { Host: `127.0.0.1:${port}`, ...headers } }, res => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => resolve({ status: res.statusCode, headers: res.headers,
          bytes: Buffer.concat(chunks), text: Buffer.concat(chunks).toString() }));
        res.on('error', reject);
      });
      req.on('error', reject); if (body !== undefined) req.write(body); req.end();
    });
  }
  return { gateway, upstream, requests, request, dir, widgetDir, config, stateFile, port,
    origin: `http://127.0.0.1:${port}` };
}

async function pair(f, options = {}) {
  const owner = JSON.parse(fs.readFileSync(f.stateFile, 'utf8'));
  const { headers = {}, ...rest } = options;
  const response = await f.request('/login', { method: 'POST', headers: {
    'Content-Type': 'application/x-www-form-urlencoded', Origin: f.origin, ...headers },
    body: `code=${owner.pairing_code}`, ...rest });
  return { response, code: owner.pairing_code, cookie: response.headers['set-cookie']?.[0].split(';')[0] };
}

test('even an entropy collision cannot reissue an already consumed one-time code', async t => {
  // Force the otherwise astronomically rare RNG collision; HTTP/session/storage remain real.
  const crypto = require('node:crypto');
  const randomInt = crypto.randomInt;
  const draws = [11111111, 22222222, 11111111, 33333333];
  crypto.randomInt = () => { assert.ok(draws.length, 'bounded random draws'); return draws.shift(); };
  try {
    const f = await fixture(t);
    assert.equal((await pair(f)).response.status, 303);
    assert.equal((await pair(f)).response.status, 303);
    assert.equal(JSON.parse(fs.readFileSync(f.stateFile, 'utf8')).pairing_code, '33333333');
    const replay = await f.request('/login', { method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'code=11111111' });
    assert.equal(replay.status, 401);
  } finally { crypto.randomInt = randomInt; }
});

test('duplicate Host headers and parser failures receive non-cacheable fail-closed responses', async t => {
  const f = await fixture(t);
  const net = require('node:net');
  async function raw(payload) {
    const socket = net.connect(f.port, '127.0.0.1');
    t.after(() => socket.destroy());
    await once(socket, 'connect');
    const chunks = []; socket.on('data', b => chunks.push(b)); socket.on('error', () => {});
    const finished = once(socket, 'close'); socket.end(payload); await finished;
    return Buffer.concat(chunks).toString();
  }
  const duplicate = await raw(`GET / HTTP/1.1\r\nHost: 127.0.0.1:${f.port}\r\nHost: evil.test\r\nConnection: close\r\n\r\n`);
  assert.match(duplicate, /^HTTP\/1.1 403/);
  const malformed = await raw(`GET / HTTP/1.1\r\nHost: 127.0.0.1:${f.port}\r\nBad Header: value\r\n\r\n`);
  assert.match(malformed, /^HTTP\/1.1 400/);
  assert.match(malformed, /Cache-Control: no-store/i);
  assert.match(malformed, /X-Content-Type-Options: nosniff/i);
  assert.match(malformed, /Referrer-Policy: no-referrer/i);
  assert.equal(f.requests.length, 0);
});

test('CLI validates arguments and runs a real opt-in gateway without logging the core secret', { timeout: 10000 }, async t => {
  const { spawn, spawnSync } = require('node:child_process');
  const script = path.resolve(__dirname, '../tools/phone-server.cjs');
  const help = spawnSync(process.execPath, [script, '--help'], { cwd: SCRATCH, encoding: 'utf8' });
  assert.equal(help.status, 0);
  assert.match(help.stdout, /--bind.*--subnet/);
  for (const args of [[], ['--bind', '0.0.0.0', '--subnet', '192.168.1.0/24'], ['--unknown', SECRET],
    ['--bind', '127.0.0.1', '--bind', '127.0.0.2'], ['--port', 'not-a-port'], ['--subnet']]) {
    const invalid = spawnSync(process.execPath, [script, ...args], { cwd: SCRATCH, encoding: 'utf8' });
    assert.equal(invalid.status, 1, JSON.stringify(args));
    assert.ok(!(invalid.stderr + invalid.stdout).includes(SECRET));
  }
  const f = await fixture(t);
  const configPath = path.join(f.dir, 'cli-config.json');
  fs.writeFileSync(configPath, JSON.stringify(f.config));
  const stateFile = path.join(f.dir, 'cli-access.json');
  const child = spawn(process.execPath, [script, '--bind', '127.0.0.1', '--subnet', '127.0.0.0/8',
    '--port', '0', '--config', configPath, '--state-file', stateFile], { cwd: SCRATCH, windowsHide: true });
  let output = '', errors = '';
  child.stdout.on('data', b => { output += b; }); child.stderr.on('data', b => { errors += b; });
  t.after(async () => { if (child.exitCode === null && child.signalCode === null) { child.kill(); await once(child, 'exit'); } });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { cleanup(); reject(new Error('CLI did not announce pairing.')); }, 3000);
    const inspect = () => { if (/Pairing code: [0-9]{8}/.test(output)) { cleanup(); resolve(); } };
    const exited = () => { cleanup(); reject(new Error('CLI exited before readiness: ' + errors)); };
    function cleanup() { clearTimeout(timer); child.stdout.off('data', inspect); child.off('exit', exited); }
    child.stdout.on('data', inspect); child.once('exit', exited); inspect();
  });
  const owner = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  assert.equal(owner.pid, child.pid);
  assert.match(output, /HTTP is not encrypted/);
  const login = await fetch(new URL('login', owner.url), { method: 'POST', redirect: 'manual',
    headers: { Origin: new URL(owner.url).origin }, body: new URLSearchParams({ code: owner.pairing_code }) });
  assert.equal(login.status, 303);
  const cookie = login.headers.getSetCookie()[0].split(';')[0];
  const health = await fetch(new URL('api/health?token=attacker', owner.url), { headers: { Cookie: cookie } });
  assert.equal(health.status, 200); await health.arrayBuffer();
  const refreshed = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  assert.notEqual(refreshed.pairing_code, owner.pairing_code);
  assert.ok(!(output + errors + JSON.stringify(refreshed)).includes(SECRET));
  assert.equal(f.requests.at(-1).headers.authorization, `Bearer ${SECRET}`);
  child.kill(); await once(child, 'exit');
  await assert.rejects(fetch(owner.url));
});

test('Promise lifecycle starts only the configured listener and removes its own owner file on stop', async t => {
  const f = await fixture(t);
  const { createPhoneServer } = gatewayModule();
  const stateFile = path.join(f.dir, 'promise-owner.json');
  const server = createPhoneServer({ bind: '127.0.0.1', subnet: '127.0.0.0/8', port: 0,
    config: f.config, stateFile, widgetDir: f.widgetDir });
  t.after(() => { server.close(); server.closeAllConnections(); });
  assert.equal(server.listening, false);
  assert.equal(typeof server.start, 'function');
  assert.equal(typeof server.stop, 'function');
  let announcement;
  server.once('pairing', owner => { announcement = owner; });
  assert.equal(await server.start(), server);
  assert.equal(server.address().address, '127.0.0.1');
  const owner = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  assert.deepEqual(announcement, owner);
  assert.ok(!JSON.stringify(announcement).includes(SECRET));
  await server.stop();
  assert.equal(server.listening, false);
  assert.equal(fs.existsSync(stateFile), false);
  await server.stop();
  const bad = createPhoneServer({ bind: '127.0.0.1', subnet: '127.0.0.0/8', port: 0,
    config: f.config, stateFile: f.widgetDir });
  await assert.rejects(bad.start(), /owner|access|publish/i);
  assert.equal(bad.listening, false);
});

test('authenticated index exposes a same-origin disconnect form before the main closes', async t => {
  const f = await fixture(t);
  const form = '<form class="phone-session" method="post" action="/logout"><button type="submit">Disconnect phone</button></form>';
  assert.ok(!(await f.request('/')).text.includes(form));
  const { cookie } = await pair(f);
  const index = await f.request('/', { headers: { Cookie: cookie } });
  assert.ok(index.text.includes(form + '</main>'));
  const disconnected = await f.request('/logout', { method: 'POST', headers: { Cookie: cookie, Origin: f.origin } });
  assert.equal(disconnected.status, 303);
  assert.equal((await f.request('/api/state', { headers: { Cookie: cookie } })).status, 401);
});

test('media errors preserve safe status and ranges but never upstream error bodies or redirects', { timeout: 5000 }, async t => {
  const f = await fixture(t, { gateway: { upstreamTimeoutMs: 120 }, upstream(req, res) {
    const id = req.url.split('/').at(-1);
    if (id === '7') return;
    if (id === '3') { res.writeHead(416, { 'Content-Type': 'application/json', 'Content-Range': 'bytes */100' }); res.end(SECRET); return; }
    if (id === '4') { res.writeHead(404, { 'Content-Type': 'application/json' }); res.end(SECRET); return; }
    if (id === '2') { res.writeHead(302, { Location: 'http://evil.test/' }); res.end(SECRET); return; }
    if (id === '5') { res.writeHead(206, { 'Content-Type': 'video/mp4', 'Content-Range': 'invalid' }); res.end('test'); return; }
    if (id === '6') { res.writeHead(200, { 'Content-Type': 'video/mp4', 'Content-Encoding': 'gzip' }); res.end('test'); return; }
    res.writeHead(200, { 'Content-Type': 'text/html' }); res.end('<script>' + SECRET + '</script>');
  } });
  const { cookie } = await pair(f);
  for (const [id, status] of [[3, 416], [4, 404], [1, 502], [2, 502], [5, 502], [6, 502], [7, 502]]) {
    const response = await f.request(`/api/video/${id}`, { headers: { Cookie: cookie } });
    assert.equal(response.status, status, String(id));
    assert.ok(!response.text.includes(SECRET));
    assert.equal(response.headers.location, undefined);
    if (id === 3) assert.equal(response.headers['content-range'], 'bytes */100');
  }
});

test('request bodies and trickling core JSON have absolute deadlines with upstream cleanup', { timeout: 5000 }, async t => {
  let upstreamClosed = false;
  const f = await fixture(t, { gateway: { bodyTimeoutMs: 120, upstreamTimeoutMs: 120, headersTimeoutMs: 120 },
    upstream(req, res) {
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.write('{"padding":"');
      const timer = setInterval(() => res.write('x'), 20);
      res.on('close', () => { clearInterval(timer); upstreamClosed = true; });
    } });
  assert.equal(f.gateway.maxConnections, 32);
  const { cookie } = await pair(f);
  const response = await f.request('/api/state', { headers: { Cookie: cookie } });
  assert.equal(response.status, 502);
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(upstreamClosed, true);
  const slow = await new Promise((resolve, reject) => {
    const request = http.request({ hostname: '127.0.0.1', port: f.port, path: '/login', method: 'POST', agent: false,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': '13' } }, res => {
      res.resume(); res.on('end', () => resolve(res.statusCode));
    });
    request.on('error', reject); request.write('code=');
    t.after(() => request.destroy());
  });
  assert.equal(slow, 408);
  const net = require('node:net');
  const socket = net.connect(f.port, '127.0.0.1');
  t.after(() => socket.destroy());
  await once(socket, 'connect');
  socket.write('GET / HTTP/1.1\r\nHost:');
  const closed = new Promise(resolve => { socket.on('close', () => resolve(true)); socket.on('error', () => {}); socket.resume(); });
  const headersClosed = await Promise.race([closed, new Promise(resolve => setTimeout(() => resolve(false), 1000))]);
  assert.equal(headersClosed, true, 'partial headers cannot pin a connection');
});

test('a paused or disconnected phone backpressures and cancels its streaming upstream', { timeout: 5000 }, async t => {
  let resolveClosed, sent = 0;
  const closed = new Promise(resolve => { resolveClosed = resolve; });
  const f = await fixture(t, { upstream(req, res) {
    res.writeHead(200, { 'Content-Type': 'video/mp4' });
    res.on('close', resolveClosed);
    const chunk = Buffer.alloc(65536, 0x5a);
    function pump() {
      while (!res.destroyed && sent < 64 * 1024 * 1024) {
        sent += chunk.length;
        if (!res.write(chunk)) { res.once('drain', pump); return; }
      }
    }
    pump();
  } });
  const { cookie } = await pair(f);
  let client, response;
  await new Promise((resolve, reject) => {
    client = http.get({ hostname: '127.0.0.1', port: f.port, path: '/api/video/2', agent: false,
      headers: { Cookie: cookie } }, res => {
      response = res;
      res.once('data', chunk => { assert.ok(chunk.length > 0); res.pause(); resolve(); });
      res.on('error', () => {});
    });
    client.on('error', reject);
  });
  t.after(() => { response.destroy(); client.destroy(); });
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.ok(sent < 64 * 1024 * 1024, `upstream remained bounded: ${sent} bytes`);
  response.destroy(); client.destroy();
  const cleaned = await Promise.race([closed.then(() => true), new Promise(resolve => setTimeout(() => resolve(false), 600))]);
  assert.equal(cleaned, true, 'phone disconnect must close upstream, not leak a reader');
});

test('media is streamed byte-exact with single Range and HEAD while private headers stay upstream', async t => {
  const bytes = Buffer.from(Array.from({ length: 65536 }, (_, i) => i % 251));
  const f = await fixture(t, { upstream(req, res) {
    assert.equal(req.headers.authorization, `Bearer ${SECRET}`);
    assert.equal(req.headers.cookie, undefined);
    assert.ok(!req.url.includes('?'));
    const mime = req.url.startsWith('/api/video/') ? 'video/mp4' : 'image/jpeg';
    res.setHeader('Content-Type', mime);
    res.setHeader('Set-Cookie', 'upstream_private=do-not-forward');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Accept-Ranges', 'bytes');
    const range = req.headers.range;
    if (range) {
      assert.equal(range, 'bytes=100-999');
      res.writeHead(206, { 'Content-Range': `bytes 100-999/${bytes.length}`, 'Content-Length': '900' });
      res.end(req.method === 'HEAD' ? undefined : bytes.subarray(100, 1000));
    } else {
      res.setHeader('Content-Length', String(bytes.length)); res.end(req.method === 'HEAD' ? undefined : bytes);
    }
  } });
  const { cookie } = await pair(f);
  const headers = { Cookie: cookie };
  assert.equal((await f.request('/api/video/2')).status, 401);
  const image = await f.request('/api/image/1?token=attacker', { headers });
  assert.equal(image.status, 200); assert.deepEqual(image.bytes, bytes);
  assert.equal(image.headers['content-type'], 'image/jpeg');
  assert.equal(image.headers['set-cookie'], undefined);
  assert.equal(image.headers['access-control-allow-origin'], undefined);
  const video = await f.request('/api/video/2?token=', { headers: { ...headers, Range: 'bytes=100-999' } });
  assert.equal(video.status, 206); assert.deepEqual(video.bytes, bytes.subarray(100, 1000));
  assert.equal(video.headers['content-range'], `bytes 100-999/${bytes.length}`);
  assert.equal(video.headers['content-length'], '900');
  const head = await f.request('/api/video/2', { method: 'HEAD', headers });
  assert.equal(head.status, 200); assert.equal(head.bytes.length, 0);
  assert.equal(head.headers['content-length'], String(bytes.length));
  for (const route of ['/api/video/abc', '/api/image/1/..', '/api/image/%31']) {
    assert.equal((await f.request(route, { headers })).status, 404);
  }
  for (const range of ['bytes=0-1,3-4', 'bytes=x-y', 'items=1-2']) {
    assert.equal((await f.request('/api/video/2', { headers: { ...headers, Range: range } })).status, 400);
  }
  assert.equal(f.requests.length, 3);
});

test('only vote and skip accept bounded JSON POSTs, preserving core status and ignoring supplied tokens', async t => {
  const bodies = [];
  const f = await fixture(t, { async upstream(req, res) {
    assert.equal(req.headers.authorization, `Bearer ${SECRET}`);
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    bodies.push(Buffer.concat(chunks));
    res.writeHead(req.url === '/api/vote' ? 409 : 200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: req.url === '/api/skip', error: req.url === '/api/vote' ? 'Stale pair.' : undefined }));
  } });
  const { cookie } = await pair(f);
  const headers = { Cookie: cookie, Origin: f.origin, 'Content-Type': 'application/json' };
  for (const [route, payload, status] of [['/api/vote?token=attacker', { pair_id: 'pair1', winner: 'left' }, 409],
    ['/api/skip?token=', { pair_id: 'pair1', failed_ids: ['2'] }, 200]]) {
    const body = JSON.stringify(payload);
    const response = await f.request(route, { method: 'POST', headers, body });
    assert.equal(response.status, status);
    assert.equal(bodies.at(-1).toString(), body);
    assert.ok(!f.requests.at(-1).url.includes('?'));
  }
  for (const [body, status, extra] of [['{invalid', 400, {}], ['[]', 400, {}],
    ['{}', 415, { 'Content-Type': 'text/plain' }], ['{"x":"' + 'x'.repeat(4096) + '"}', 413, {}]]) {
    assert.equal((await f.request('/api/vote', { method: 'POST', headers: { ...headers, ...extra }, body })).status, status);
  }
  for (const route of ['/api/export', '/api/config', '/api/admin', '/api/health', '/api/state']) {
    assert.equal((await f.request(route, { method: 'POST', headers, body: '{}' })).status, 404);
  }
  assert.equal(f.requests.length, 2);
  assert.equal((await f.request('/login', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'code=' + '0'.repeat(4096) })).status, 413);
  assert.equal((await f.request('/api/skip', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status, 401);
});

test('JSON proxy fails closed above 1 MiB or on residual secrets, redirects and unsafe URLs', async t => {
  const f = await fixture(t, { upstream(req, res) {
    const mode = new URL(req.url, 'http://127.0.0.1').searchParams.get('pair_id');
    res.setHeader('Content-Type', 'application/json');
    if (mode === 'huge') { res.end(JSON.stringify({ padding: 'x'.repeat(1024 * 1024) })); return; }
    if (mode === 'redirect') { res.writeHead(302, { Location: 'http://evil.test/' }); res.end('{}'); return; }
    if (mode === 'leak') { res.end(JSON.stringify({ error: SECRET })); return; }
    if (mode === 'escaped') { res.end('{"error":"' + [...SECRET].map(c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')).join('') + '"}'); return; }
    if (mode === 'external') { res.end(JSON.stringify({ top: [{ url: 'http://evil.test/api/image/1' }] })); return; }
    if (mode === 'privatepath') { res.end(JSON.stringify({ pair: { left: { url: '/api/export?token=' + SECRET } } })); return; }
    res.end('not JSON ' + SECRET);
  } });
  const { cookie } = await pair(f);
  for (const mode of ['huge', 'leak', 'escaped', 'external', 'privatepath', 'redirect', 'malformed']) {
    const response = await f.request('/api/state?pair_id=' + mode, { headers: { Cookie: cookie } });
    assert.equal(response.status, 502, mode);
    assert.ok(!response.text.includes(SECRET));
    assert.equal(response.headers.location, undefined);
    assert.ok(response.bytes.length < 200);
  }
});

test('state and health proxy only to loopback using server-side Bearer and token-free media URLs', async t => {
  const item = (id, kind) => ({ id: String(id), kind, url: `/api/${kind}/${id}?token=${SECRET}`,
    poster_url: `http://127.0.0.1:CORE/api/image/${id}?token=${SECRET}` });
  let corePort;
  const f = await fixture(t, { upstream(req, res) {
    assert.equal(req.headers.authorization, `Bearer ${SECRET}`);
    assert.equal(req.headers.cookie, undefined);
    assert.equal(req.headers.origin, undefined);
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Content-Type', 'application/json');
    const data = req.url === '/api/health' ? { ok: true, scanning: false } : {
      pair: { pair_id: 'pair1', left: item(1, 'image'), right: item(2, 'video') }, top: [item(3, 'video')], stats: { votes: 4 } };
    res.end(JSON.stringify(data).replaceAll(':CORE/', `:${corePort}/`));
  } });
  corePort = f.upstream.address().port;
  f.config.port = 1; f.config.api_token = 'mutated';
  const { cookie } = await pair(f);
  const headers = { Cookie: cookie, Authorization: 'Bearer attacker', Origin: f.origin };
  const response = await f.request('/api/state?token=attacker&pair_id=pair1', { headers });
  assert.equal(response.status, 200);
  assert.equal(f.requests[0].url, '/api/state?pair_id=pair1');
  const data = JSON.parse(response.text);
  assert.equal(data.pair.left.url, '/api/image/1');
  assert.equal(data.pair.right.url, '/api/video/2');
  assert.equal(data.pair.right.poster_url, '/api/image/2');
  assert.equal(data.top[0].url, '/api/video/3');
  assert.equal(data.top[0].poster_url, '/api/image/3');
  assert.ok(!response.text.includes(SECRET));
  assert.ok(!response.text.includes('token='));
  assert.equal(response.headers['access-control-allow-origin'], undefined);
  assert.equal((await f.request('/api/health?token=', { headers })).status, 200);
  assert.equal(f.requests.at(-1).url, '/api/health');
  for (const route of ['/api/state?pair_id=', '/api/state?pair_id=a&pair_id=b', '/api/state?admin=1']) {
    assert.equal((await f.request(route, { headers })).status, 400);
  }
  assert.equal(f.requests.length, 2);
});

test('private core configuration is validated and snapshots cannot redirect its loopback authority', async t => {
  const { createPhoneServer } = gatewayModule();
  const base = { bind: '127.0.0.1', subnet: '127.0.0.0/8', port: 0 };
  for (const config of [undefined, {}, { api_token: 'short', port: 47831 },
    { api_token: SECRET, port: 0 }, { api_token: SECRET, port: '47831' },
    { api_token: SECRET, port: 65536 }, { api_token: SECRET + '\r\nX-Secret: yes', port: 47831 },
    { api_token: SECRET, port: 47831, host: '192.168.1.2' },
    { api_token: SECRET, port: 47831, host: 'localhost' }]) {
    assert.throws(() => createPhoneServer({ ...base, config, configPath: 'Z:/nonexistent-gateway-test.json' }), error => {
      assert.ok(!error.message.includes(SECRET)); return /config|token|port|loopback/i.test(error.message);
    });
  }
  const f = await fixture(t);
  const configPath = path.join(f.dir, 'fixture-config.json');
  fs.writeFileSync(configPath, JSON.stringify(f.config));
  const loaded = createPhoneServer({ ...base, configPath, stateFile: path.join(f.dir, 'not-started.json') });
  loaded.close();
  fs.writeFileSync(configPath, '{"api_token":"' + SECRET + '",invalid}');
  assert.throws(() => createPhoneServer({ ...base, configPath }), error => {
    assert.ok(!error.message.includes(SECRET)); return /config/i.test(error.message);
  });
});

test('sessions have fixed 24-hour expiry and evict the oldest beyond sixteen', async t => {
  let now = 1000;
  const f = await fixture(t, { gateway: { now: () => now } });
  const first = await pair(f);
  const get = cookie => f.request('/runtime.js', { headers: { Cookie: cookie } });
  now += 23 * 60 * 60 * 1000;
  assert.equal((await get(first.cookie)).status, 200);
  now += 60 * 60 * 1000;
  assert.equal((await get(first.cookie)).status, 401);
  const cookies = [];
  for (let n = 0; n < 17; n++) cookies.push((await pair(f)).cookie);
  assert.equal((await get(cookies[0])).status, 401);
  for (const cookie of cookies.slice(1)) assert.equal((await get(cookie)).status, 200);
  assert.equal((await f.request('/runtime.js?session=' + cookies[1].split('=')[1])).status, 401);
});

test('paired browsers receive only public widget assets with token-free same-origin defaults', async t => {
  const f = await fixture(t);
  assert.equal((await f.request('/runtime.js')).status, 401);
  const { cookie } = await pair(f);
  const headers = { Cookie: cookie };
  const index = await f.request('/', { headers });
  assert.equal(index.status, 200);
  assert.ok(!index.text.includes('<form method="post" action="/login"'));
  assert.match(index.text, /href="phone\.css"/);
  const nonce = index.headers['content-security-policy'].match(/'nonce-([^']+)'/)[1];
  assert.ok(index.text.includes(`<script nonce="${nonce}">window.fixtureBoot=true;`));
  const defaults = await f.request('/defaults.js?token=ignored', { headers });
  assert.equal(defaults.status, 200);
  assert.match(defaults.text, /window.ImageRankerPhoneMode\s*=\s*true/);
  assert.match(defaults.text, /endpoint:\s*location.origin/);
  assert.match(defaults.text, /apiToken:\s*''/);
  for (const route of ['/runtime.js', '/style.css', '/phone.css', '/translation.json', '/resources/icon.svg', '/index.html']) {
    const asset = await f.request(route, { headers });
    assert.equal(asset.status, 200, route);
    assert.ok(!asset.text.includes(SECRET));
    assert.equal(asset.headers['cache-control'], 'no-store');
  }
  for (const route of ['/config.json', '/.git/config', '/data/phone-access.json', '/widget/defaults.js', '/resources/',
    '/%2e%2e/config.json', '/resources/../defaults.js', '/api/export', '/api/config', '/api/admin', '//defaults.js']) {
    assert.equal((await f.request(route, { headers })).status, 404, route);
  }
  assert.equal(f.requests.length, 0);
});

test('failed pairing guesses are limited per IP and globally for five minutes', async t => {
  let now = 1000000;
  const f = await fixture(t, { gateway: { now: () => now } });
  const attempt = localAddress => f.request('/login', { method: 'POST', localAddress,
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'code=not-eight-digits' });
  for (let n = 0; n < 5; n++) assert.equal((await attempt('127.0.0.1')).status, 401);
  assert.equal((await pair(f)).response.status, 429);
  for (let ip = 2; ip <= 6; ip++) {
    for (let n = 0; n < 5; n++) assert.equal((await attempt(`127.0.0.${ip}`)).status, 401);
  }
  const blocked = await attempt('127.0.0.7');
  assert.equal(blocked.status, 429);
  assert.equal(blocked.headers['retry-after'], '300');
  now += 300000;
  assert.equal((await pair(f)).response.status, 303);
  assert.equal(f.requests.length, 0);
});

test('browser POSTs reject foreign/null origins and cross-site fetch metadata without consuming a code', async t => {
  const f = await fixture(t);
  const initialCode = JSON.parse(fs.readFileSync(f.stateFile, 'utf8')).pairing_code;
  for (const headers of [{ Origin: 'http://evil.test' }, { Origin: 'null' },
    { Origin: f.origin + '/' }, { 'Sec-Fetch-Site': 'cross-site' },
    { Origin: undefined, 'Sec-Fetch-Site': 'same-origin' }]) {
    const supplied = { 'Content-Type': 'application/x-www-form-urlencoded', Origin: f.origin, ...headers };
    for (const key of Object.keys(supplied)) if (supplied[key] === undefined) delete supplied[key];
    const response = await f.request('/login', { method: 'POST', headers: supplied, body: `code=${initialCode}` });
    assert.equal(response.status, 403, JSON.stringify(headers));
    assert.equal(JSON.parse(fs.readFileSync(f.stateFile, 'utf8')).pairing_code, initialCode);
  }
  const { cookie } = await pair(f);
  for (const route of ['/logout', '/api/vote', '/api/skip']) {
    assert.equal((await f.request(route, { method: 'POST', headers: { Cookie: cookie, Origin: 'http://evil.test' }, body: '{}' })).status, 403);
  }
  assert.equal((await f.request('/logout', { method: 'POST', headers: { Cookie: cookie } })).status, 303);
});

test('one-time pairing rotates the private owner code and issues a revocable IP-bound cookie', async t => {
  const f = await fixture(t);
  const owner = JSON.parse(fs.readFileSync(f.stateFile, 'utf8'));
  assert.deepEqual(Object.keys(owner).sort(), ['pairing_code', 'pid', 'url']);
  assert.match(owner.pairing_code, /^\d{8}$/);
  assert.equal(owner.url, f.origin + '/');
  assert.equal(owner.pid, process.pid);
  assert.ok(!JSON.stringify(owner).includes(SECRET));
  const { response, cookie, code } = await pair(f);
  assert.equal(response.status, 303);
  assert.equal(response.headers.location, '/');
  assert.match(response.headers['set-cookie'][0], /HttpOnly/);
  assert.match(response.headers['set-cookie'][0], /SameSite=Strict/);
  assert.match(response.headers['set-cookie'][0], /Max-Age=86400/);
  assert.match(cookie, /^image_ranker_phone=[a-f0-9]{64}$/);
  assert.ok(!response.headers['set-cookie'][0].includes('Secure'));
  assert.notEqual(JSON.parse(fs.readFileSync(f.stateFile, 'utf8')).pairing_code, code);
  const replay = await f.request('/login', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: `code=${code}` });
  assert.equal(replay.status, 401);
  assert.equal((await f.request('/logout', { method: 'POST', headers: { Cookie: cookie }, localAddress: '127.0.0.2' })).status, 401);
  const logout = await f.request('/logout', { method: 'POST', headers: { Cookie: cookie, Origin: f.origin } });
  assert.equal(logout.status, 303);
  assert.match(logout.headers['set-cookie'][0], /Max-Age=0/);
  assert.equal((await f.request('/logout', { method: 'POST', headers: { Cookie: cookie } })).status, 401);
  assert.equal(f.requests.length, 0);
});

test('network admission requires an explicit private bind inside its subnet and exact Host', async t => {
  const { createPhoneServer } = gatewayModule();
  const base = { bind: '127.0.0.1', subnet: '127.0.0.0/8', port: 0,
    config: { host: '127.0.0.1', port: 47831, api_token: SECRET } };
  for (const override of [
    { bind: '0.0.0.0' }, { bind: '8.8.8.8', subnet: '8.8.8.0/24' },
    { bind: 'localhost' }, { bind: '::1' }, { bind: '192.168.1.3', subnet: '10.0.0.0/8' },
    { subnet: undefined }, { subnet: '0.0.0.0/0' }, { subnet: '127.0.0.0/99' },
    { subnet: '127.0.0.1/8' }, { port: -1 }, { port: 65536 },
  ]) assert.throws(() => createPhoneServer({ ...base, ...override }), /bind|subnet|port/i);
  for (const [bind, subnet] of [['10.1.2.3', '10.1.2.0/24'], ['172.16.1.2', '172.16.0.0/16'],
    ['192.168.2.5', '192.168.2.0/24'], ['127.0.0.2', '127.0.0.0/8']]) {
    createPhoneServer({ ...base, bind, subnet }).close();
  }
  const f = await fixture(t);
  for (const host of ['evil.test', '127.0.0.1', `localhost:${f.port}`, `${f.origin.slice(7)}.evil.test`, `127.0.0.2:${f.port}`]) {
    assert.equal((await f.request('/', { headers: { Host: host } })).status, 403);
  }
  const restricted = await fixture(t, { gateway: { subnet: '127.0.0.1/32' } });
  assert.equal((await restricted.request('/', { localAddress: '127.0.0.2' })).status, 403);
  assert.equal(f.requests.length, 0);
});

test('unauthenticated HTTP shows only a login form, not core metadata', async t => {
  const f = await fixture(t);
  const root = await f.request('/');
  assert.equal(root.status, 200);
  assert.match(root.text, /<form[^>]*method="post"[^>]*action="\/login"/i);
  assert.match(root.text, /name="code"/);
  assert.ok(!root.text.includes(SECRET));
  const api = await f.request('/api/state?token=attacker');
  assert.equal(api.status, 401);
  assert.deepEqual(JSON.parse(api.text), { error: 'Authentication required.' });
  assert.equal(f.requests.length, 0);
  for (const response of [root, api]) {
    assert.equal(response.headers['cache-control'], 'no-store');
    assert.equal(response.headers['referrer-policy'], response===root?'same-origin':'no-referrer');
    assert.equal(response.headers['x-content-type-options'], 'nosniff');
    assert.equal(response.headers['x-frame-options'], 'DENY');
    assert.match(response.headers['content-security-policy'], /frame-ancestors 'none'/);
    assert.match(response.headers['content-security-policy'], /form-action 'self'/);
    assert.equal(response.headers['access-control-allow-origin'], undefined);
  }
});
