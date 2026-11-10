import { cp, mkdir, readFile, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
const out = path.join(root, 'dist');
const analysis = JSON.parse(await readFile(path.join(root, 'public/data/analysis.json'), 'utf8'));
if (!analysis.groups?.length || !Object.keys(analysis.distances || {}).length) throw Error('Prepare real routing data before publishing.');
await mkdir(out, { recursive: true });
for (const asset of ['index.html', 'styles.css', 'app.js', 'src', 'public/data', 'public/vendor']) {
  await cp(path.join(root, asset), path.join(out, asset), { recursive: true });
}
// Fail rather than silently publish a workbook or source graph from an old build.
async function inspect(directory) {
  for (const name of await readdir(directory)) {
    const file = path.join(directory, name);
    if ((await stat(file)).isDirectory()) await inspect(file);
    else {
      // Font glyphs are also Protocol Buffers; allow only the vendored font ranges,
      // while continuing to reject raw OSM .pbf files everywhere else.
      const relative = path.relative(out, file).split(path.sep).join('/');
      const fontGlyph = /^public\/vendor\/fonts\/[^/]+\/\d+-\d+\.pbf$/.test(relative);
      if ((/\.(xlsx?|pbf|pyt|py)$/i.test(name) && !fontGlyph) || name === 'network.json') {
        throw Error(`Private/source file found in build: ${file}`);
      }
    }
  }
}
await inspect(out);
console.log('Static site ready in dist/ (aggregate data only).');
