import { mkdir, copyFile } from 'node:fs/promises';
const root = new URL('../', import.meta.url);
await mkdir(new URL('public/vendor/', root), { recursive: true });
for (const [source, target] of [
  ['dist/maplibre-gl.js', 'maplibre-gl.js'], ['dist/maplibre-gl.css', 'maplibre-gl.css'],
  ['LICENSE.txt', 'maplibre-LICENSE.txt'],
]) {
  await copyFile(new URL(`node_modules/maplibre-gl/${source}`, root), new URL(`public/vendor/${target}`, root));
}
console.log('Vendored MapLibre 5.3.0 assets and licence.');
