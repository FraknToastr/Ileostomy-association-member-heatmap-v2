import test from 'node:test';
import assert from 'node:assert/strict';
import { copyFile, lstat, mkdir, mkdtemp, readFile, readdir, rename, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { buildSite } from '../scripts/build.mjs';

// Deliberately nonconsecutive: route selection must follow analysis.groups.
const groups = [1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12].map(id => ({ id: `group-${id}` }));
const dataFiles = ['analysis.json', 'manifest.json', 'members.geojson', 'membership-report.json', 'snap-report.json',
  ...groups.map(group => `routes/${group.id}.geojson`)];
const assets = ['index.html', 'app.js', 'styles.css', 'src/analysis.js', 'src/map-analysis.js', 'src/map-style.js', 'src/exports.js',
  'public/vendor/maplibre-gl.js', 'public/vendor/maplibre-gl.css', 'public/vendor/maplibre-LICENSE.txt'];
const expected = [...assets, ...dataFiles.map(file => `public/data/${file}`)].sort();
const collection = features => ({ type: 'FeatureCollection', features });

async function put(root, name, value) {
  const file = path.join(root, name);
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, typeof value === 'string' ? value : JSON.stringify(value));
}

async function fixture(t) {
  const root = await mkdtemp(path.join(tmpdir(), 'heatmap-build-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const file of assets) await put(root, file, `fixture: ${file}\n`);
  await put(root, 'public/data/analysis.json', {
    schemaVersion: 1, groups,
    distances: { 'suburb-1': Object.fromEntries(groups.map(group => [group.id, { status: 'ok', distanceM: 1000 }])) },
  });
  await put(root, 'public/data/members.geojson', collection([
    { type: 'Feature', properties: { id: 'suburb-1', members: 3 }, geometry: { type: 'Point', coordinates: [138.6, -34.9] } },
  ]));
  await put(root, 'public/data/membership-report.json', {
    totals: { joinedMembers: 3, unresolvedMembers: 1, memberRecords: 4 }, unresolved: [{ count: 1 }],
  });
  await put(root, 'public/data/manifest.json', { schemaVersion: 1, files: dataFiles.filter(file => file !== 'manifest.json') });
  await put(root, 'public/data/snap-report.json', { review: [], snaps: { 'suburb-1': { status: 'ok' } } });
  for (const group of groups) await put(root, `public/data/routes/${group.id}.geojson`, collection([]));
  return root;
}

async function outputFiles(root, relative = '') {
  const files = [];
  for (const entry of await readdir(path.join(root, relative), { withFileTypes: true })) {
    const name = relative ? `${relative}/${entry.name}` : entry.name;
    assert.equal(entry.isSymbolicLink(), false, name);
    if (entry.isDirectory()) files.push(...await outputFiles(root, name));
    else {
      assert.ok(entry.isFile(), name);
      files.push(name);
    }
  }
  return files.sort();
}

test('build copies exactly the app, 16 prepared data files and three vendor files; cleans stale output', async t => {
  const root = await fixture(t);
  for (const file of [
    'members.xlsx', 'south-australia.osm.pbf', '.generated/network.json',
    'public/data/network.json', 'public/data/members.xlsx', 'public/data/raw.pbf', 'public/data/extra.json',
    'public/data/routes/group-9.geojson', 'public/data/nested/source.py',
    'public/vendor/fonts/Open Sans Regular/0-255.pbf', 'public/vendor/maplibre-gl.js.map',
    'public/unselected.js', 'src/source.py', 'src/network.json', 'src/raw.pbf', 'src/nested/unselected.js',
    'dist/stale.js', 'dist/src/retired.js', 'dist/public/data/stale.json', 'dist/members.xlsx',
    'dist/public/vendor/fonts/Open Sans Regular/0-255.pbf',
  ]) await put(root, file, 'must not be published');
  await symlink(path.join(root, 'members.xlsx'), path.join(root, 'public/data/linked.json'));
  await symlink(path.join(root, '.generated'), path.join(root, 'public/vendor/unselected'));
  // Cleanup must unlink stale output links without traversing their targets.
  await symlink(path.join(root, '.generated'), path.join(root, 'dist/private'));

  const out = await buildSite(root);
  assert.equal(out, path.join(root, 'dist'));
  assert.deepEqual(await outputFiles(out), expected);
  assert.equal((await outputFiles(path.join(out, 'public/data'))).length, 16);
  for (const file of expected) assert.deepEqual(await readFile(path.join(out, file)), await readFile(path.join(root, file)), file);
  assert.equal(await readFile(path.join(root, '.generated/network.json'), 'utf8'), 'must not be published');

  await put(root, 'app.js', 'updated app');
  await put(root, 'dist/another-stale.pbf', 'stale');
  await buildSite(root);
  assert.deepEqual(await outputFiles(out), expected);
  assert.equal(await readFile(path.join(out, 'app.js'), 'utf8'), 'updated app');
});

test('missing required files fail before copying any output', async t => {
  for (const file of expected) await t.test(file, async t => {
    const root = await fixture(t);
    await rm(path.join(root, file));
    await assert.rejects(buildSite(root), { code: 'ENOENT' });
    await assert.rejects(lstat(path.join(root, 'dist')), { code: 'ENOENT' });
  });
});

test('incomplete prepared data and unsafe group IDs fail', async t => {
  const cases = [
    ['analysis.json', {}, /routing data/],
    ['analysis.json', { schemaVersion: 1, groups, distances: {} }, /routing data/],
    ['analysis.json', { schemaVersion: 1, groups: [{ id: '../private' }], distances: { a: {} } }, /group IDs/],
    ['analysis.json', { schemaVersion: 1, groups: [groups[0], groups[0]], distances: { a: {} } }, /group IDs/],
    ['members.geojson', collection([]), /members.geojson is incomplete/],
    ['members.geojson', collection([{ properties: { members: '3' } }]), /members.geojson is incomplete/],
    ['membership-report.json', { totals: { joinedMembers: 2, unresolvedMembers: 1, memberRecords: 3 } }, /totals do not reconcile/],
    ['membership-report.json', { totals: { joinedMembers: 3, unresolvedMembers: 1, memberRecords: 5 } }, /totals do not reconcile/],
    ['manifest.json', { schemaVersion: 1, files: ['members.geojson'] }, /manifest.json is incomplete/],
    ['snap-report.json', {}, /snap-report.json is incomplete/],
    ['routes/group-1.geojson', {}, /routes\/group-1.geojson is incomplete/],
    ['analysis.json', '{', /Invalid JSON input/],
    ['manifest.json', '', /Missing or incomplete input/],
  ];
  for (const [file, value, error] of cases) await t.test(`${file}: ${JSON.stringify(value)}`, async t => {
    const root = await fixture(t);
    await put(root, `public/data/${file}`, value);
    await assert.rejects(buildSite(root), error);
    await assert.rejects(lstat(path.join(root, 'dist')), { code: 'ENOENT' });
  });
});

test('selected symlink files and parent directories are rejected', async t => {
  for (const file of [...expected, 'src', 'public', 'public/data', 'public/data/routes', 'public/vendor']) {
    await t.test(file, async t => {
      const root = await fixture(t);
      const original = path.join(root, file);
      const target = path.join(root, 'unselected-target');
      await rename(original, target);
      await symlink(target, original);
      await assert.rejects(buildSite(root), /Symlink input is not allowed/);
      await assert.rejects(lstat(path.join(root, 'dist')), { code: 'ENOENT' });
    });
  }
});

test('directories and empty files cannot masquerade as selected assets', async t => {
  for (const file of ['app.js', 'src/analysis.js', 'public/vendor/maplibre-gl.js']) {
    for (const directory of [false, true]) await t.test(`${file}: ${directory ? 'directory' : 'empty'}`, async t => {
      const root = await fixture(t);
      await rm(path.join(root, file));
      if (directory) await mkdir(path.join(root, file));
      else await put(root, file, '');
      await assert.rejects(buildSite(root), /Missing or incomplete input/);
    });
  }
  await t.test('no source modules', async t => {
    const root = await fixture(t);
    await rm(path.join(root, 'src'), { recursive: true });
    await put(root, 'src/ignored.json', '{}');
    await assert.rejects(buildSite(root), /Missing JavaScript source files/);
  });
});

test('import has no build side effect until buildSite is called', async t => {
  const root = await fixture(t);
  const script = path.join(root, 'scripts/build.mjs');
  await mkdir(path.dirname(script));
  await copyFile(new URL('../scripts/build.mjs', import.meta.url), script);
  const { buildSite: importedBuild } = await import(pathToFileURL(script));
  assert.equal(typeof importedBuild, 'function');
  await assert.rejects(lstat(path.join(root, 'dist')), { code: 'ENOENT' });
  await importedBuild(root);
  assert.deepEqual(await outputFiles(path.join(root, 'dist')), expected);
});
