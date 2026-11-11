import { createHash } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { FONT_STACK, GLYPH_URL } from '../src/map-style.js';

// Test fixtures only: these files are ignored and never copied to the public build.
const directory = new URL(`../.generated/test-glyphs/${FONT_STACK}/`, import.meta.url);
const ranges = {
  '0-255': 'ef1f38a3f1978591e846e9eaddf8a54f7047f546fc6aaed7872cc53151a5de78',
  '256-511': '992557f9e2574cfb9a75e16c7b52832578849772b7c9a6e480d6213fb008fe5d',
  '8192-8447': 'f061e5b4e42e5925228534d8822b423a029569263db09f5ce44573600d45295f',
};
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
await mkdir(directory, { recursive: true });
for (const [range, expected] of Object.entries(ranges)) {
  const target = new URL(`${range}.pbf`, directory);
  let cached;
  try { cached = await readFile(target); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (cached && digest(cached) === expected) continue;
  const url = GLYPH_URL.replace('{fontstack}', encodeURIComponent(FONT_STACK)).replace('{range}', range);
  let bytes;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(30_000) });
      if (!response.ok) throw Error(`Glyph download failed: HTTP ${response.status} (${range})`);
      bytes = Buffer.from(await response.arrayBuffer());
      if (digest(bytes) !== expected) throw Error(`Glyph checksum mismatch (${range}); review upstream before updating fixtures.`);
      break;
    } catch (error) { if (attempt === 2) throw error; }
  }
  const temporary = new URL(`${range}.pbf.tmp`, directory);
  await writeFile(temporary, bytes);
  await rename(temporary, target);
}
console.log('Three hosted Noto Sans glyph fixtures verified in the ignored test cache.');
