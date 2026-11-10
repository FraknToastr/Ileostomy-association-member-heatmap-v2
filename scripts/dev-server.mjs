import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Serve only publishable assets. Never expose the source workbook or road data.
const root = fileURLToPath(new URL('../', import.meta.url));
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const port = Number(option('--port', process.env.PORT || 4173));
const host = option('--host', '127.0.0.1');
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.geojson': 'application/geo+json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.txt': 'text/plain; charset=utf-8' };
const allowed = (relative) => ['index.html', 'app.js', 'styles.css'].includes(relative)
  || /^(public\/(data|vendor)\/|src\/)[^\\]*$/.test(relative);

const server = http.createServer((req, res) => {
  let relative;
  try {
    relative = decodeURIComponent(new URL(req.url, 'http://localhost').pathname).replace(/^\/+/, '') || 'index.html';
  } catch { res.writeHead(400).end('Bad request'); return; }
  if (!['GET', 'HEAD'].includes(req.method)) { res.writeHead(405).end(); return; }
  if (!allowed(relative) || relative.split('/').some(p => p === '..' || p.startsWith('.'))) {
    res.writeHead(404).end('Not found'); return;
  }
  const file = path.resolve(root, relative);
  if (!file.startsWith(root)) { res.writeHead(404).end(); return; }
  fs.stat(file, (error, stat) => {
    if (error || !stat.isFile()) { res.writeHead(404).end('Not found'); return; }
    res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream',
      'Content-Length': stat.size, 'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff' });
    if (req.method === 'HEAD') res.end(); else fs.createReadStream(file).pipe(res);
  });
});
server.listen(port, host, () => console.log(`Support heatmap: http://${host}:${port}`));
server.on('error', error => { console.error(error.message); process.exitCode = 1; });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close(() => process.exit(0)));
