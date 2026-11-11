import { createServer } from 'node:http';
import { constants } from 'node:fs';
import { lstat, open, realpath } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const prefix = '/Ileostomy-association-member-heatmap-v2/';
const host = '127.0.0.1';
const { values } = parseArgs({ options: { port: { type: 'string', default: '4174' } } });
const port = Number(values.port);
if (!/^\d+$/.test(values.port) || !Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('Use --port with a port number between 1 and 65535.');
}

const dist = fileURLToPath(new URL('../dist/', import.meta.url));
if (!(await lstat(dist)).isDirectory()) throw new Error('Build dist/ before starting the Pages preview.');
const root = await realpath(dist);
const withinRoot = file => {
  const relative = path.relative(root, file);
  return relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
};
const mime = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.geojson': 'application/geo+json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.ico': 'image/x-icon',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.otf': 'font/otf',
  '.txt': 'text/plain; charset=utf-8', '.webmanifest': 'application/manifest+json',
};

const server = createServer(async (req, res) => {
  let handle;
  const reply = (status, message, headers = {}) => {
    res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8',
      'X-Content-Type-Options': 'nosniff', ...headers });
    res.end(req.method === 'HEAD' ? undefined : message);
  };
  if (!['GET', 'HEAD'].includes(req.method)) {
    reply(405, 'Method not allowed', { Allow: 'GET, HEAD' });
    return;
  }

  // Inspect the raw path: URL() would normalize dot segments before validation.
  const [rawPath] = (req.url || '').split('?');
  let pathname;
  try { pathname = decodeURIComponent(rawPath); }
  catch { reply(400, 'Bad request'); return; }
  if (!pathname.startsWith('/') || pathname.startsWith('//')
      || /[\\\x00-\x1f\x7f]/.test(pathname) || /%(?:2f|5c)/i.test(rawPath)) {
    reply(400, 'Bad request'); return;
  }
  if (pathname === prefix.slice(0, -1)) {
    reply(308, 'Redirecting', { Location: prefix + (req.url.includes('?') ? req.url.slice(req.url.indexOf('?')) : '') });
    return;
  }
  if (!pathname.startsWith(prefix)) { reply(404, 'Not found'); return; }
  const relative = pathname.slice(prefix.length) || 'index.html';
  const segments = relative.split('/');
  if (segments.some(segment => !segment || segment.startsWith('.'))
      || /\.(?:xlsx?|xlsm|xlsb|ods|pbf|osm|pyt|py)$/i.test(relative)
      || segments.at(-1).toLowerCase() === 'network.json') {
    reply(404, 'Not found'); return;
  }
  const candidate = path.resolve(root, relative);
  if (!withinRoot(candidate)) { reply(404, 'Not found'); return; }

  try {
    const file = await realpath(candidate);
    // A symlink in dist must never make a source file outside dist public.
    if (!withinRoot(file)) { reply(404, 'Not found'); return; }
    handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const stat = await handle.stat();
    if (!stat.isFile()) { await handle.close(); handle = undefined; reply(404, 'Not found'); return; }
    res.writeHead(200, {
      'Content-Type': mime[path.extname(file).toLowerCase()] || 'application/octet-stream',
      'Content-Length': stat.size, 'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff',
    });
    if (req.method === 'HEAD') { await handle.close(); handle = undefined; res.end(); }
    else {
      const stream = handle.createReadStream();
      handle = undefined;
      stream.on('error', () => res.destroy());
      res.on('close', () => stream.destroy());
      stream.pipe(res);
    }
  } catch (error) {
    await handle?.close();
    if (res.headersSent) res.destroy();
    else reply(['ENOENT', 'ENOTDIR', 'EACCES', 'ELOOP'].includes(error.code) ? 404 : 500, 'Not found');
  }
});

server.listen(port, host, () => console.log(`Pages preview: http://${host}:${port}${prefix}`));
server.on('error', error => { console.error(error.message); process.exitCode = 1; });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close(() => process.exit(0)));
