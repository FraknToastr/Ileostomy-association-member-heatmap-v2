import { copyFile, lstat, mkdir, readFile, readdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const appFiles = ['index.html', 'app.js', 'styles.css'];
const requiredSources = ['analysis.js', 'map-analysis.js', 'map-style.js', 'exports.js'];
const dataFiles = ['analysis.json', 'manifest.json', 'members.geojson', 'membership-report.json', 'snap-report.json'];
const vendorFiles = ['maplibre-gl.js', 'maplibre-gl.css', 'maplibre-LICENSE.txt'];
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const isCount = value => Number.isSafeInteger(value) && value >= 0;

export async function buildSite(root) {
  root = path.resolve(root);
  const out = path.join(root, 'dist');

  // Check every selected path component: lstat on a file alone follows linked parents.
  async function input(relative, directory = false) {
    let current = root;
    for (const part of ['', ...relative.split('/')]) {
      current = path.join(current, part);
      const info = await lstat(current);
      if (info.isSymbolicLink()) throw Error(`Symlink input is not allowed: ${current}`);
      const isDirectory = current !== path.join(root, relative) || directory;
      if (isDirectory ? !info.isDirectory() : !info.isFile() || info.size === 0) {
        throw Error(`Missing or incomplete input: ${current}`);
      }
    }
    return current;
  }

  async function json(name) {
    const file = await input(`public/data/${name}`);
    try {
      return JSON.parse(await readFile(file, 'utf8'));
    } catch (cause) {
      throw Error(`Invalid JSON input: public/data/${name}`, { cause });
    }
  }

  const analysis = await json('analysis.json');
  if (analysis?.schemaVersion !== 1 || !Array.isArray(analysis.groups) || !analysis.groups.length ||
      !isRecord(analysis.distances) || !Object.keys(analysis.distances).length) {
    throw Error('Prepare real routing data before publishing: analysis.json is incomplete.');
  }
  const groupIds = analysis.groups.map(group => group?.id);
  if (groupIds.some(id => typeof id !== 'string' || !/^group-[1-9]\d*$/.test(id)) ||
      new Set(groupIds).size !== groupIds.length) {
    throw Error('Invalid or duplicate group IDs in analysis.json.');
  }
  const routes = groupIds.map(id => `routes/${id}.geojson`);
  const members = await json('members.geojson');
  if (members?.type !== 'FeatureCollection' || !Array.isArray(members.features) || !members.features.length ||
      members.features.some(feature => !isCount(feature?.properties?.members))) {
    throw Error('Prepared members.geojson is incomplete.');
  }
  const report = await json('membership-report.json');
  const totals = report?.totals;
  const joined = members.features.reduce((sum, feature) => sum + feature.properties.members, 0);
  if (![totals?.joinedMembers, totals?.unresolvedMembers, totals?.memberRecords].every(isCount) ||
      joined !== totals.joinedMembers || joined + totals.unresolvedMembers !== totals.memberRecords) {
    throw Error('Member totals do not reconcile. Re-run data preparation before publishing.');
  }
  const manifest = await json('manifest.json');
  if (manifest?.schemaVersion !== 1 || !Array.isArray(manifest.files) ||
      [...dataFiles.filter(name => name !== 'manifest.json'), ...routes].some(name => !manifest.files.includes(name))) {
    throw Error('Prepared manifest.json is incomplete.');
  }
  const snaps = await json('snap-report.json');
  if (!Array.isArray(snaps?.review) || !isRecord(snaps?.snaps) || !Object.keys(snaps.snaps).length) {
    throw Error('Prepared snap-report.json is incomplete.');
  }
  for (const route of routes) {
    const collection = await json(route);
    // Empty collections are valid when no suburbs can reach a group.
    if (collection?.type !== 'FeatureCollection' || !Array.isArray(collection.features)) {
      throw Error(`Prepared ${route} is incomplete.`);
    }
  }

  const sourceDirectory = await input('src', true);
  const sourceFiles = (await readdir(sourceDirectory)).filter(name => name.endsWith('.js')).sort();
  if (!sourceFiles.length) throw Error('Missing JavaScript source files in src/.');
  for (const name of requiredSources) await input(`src/${name}`);
  // Ignore all unselected inputs, including extra data, raw graphs, fonts and workbooks.
  const files = [
    ...appFiles,
    ...sourceFiles.map(name => `src/${name}`),
    ...[...dataFiles, ...routes].map(name => `public/data/${name}`),
    ...vendorFiles.map(name => `public/vendor/${name}`),
  ];
  for (const file of files) await input(file);

  await rm(out, { recursive: true, force: true });
  for (const file of files) {
    const destination = path.join(out, file);
    await mkdir(path.dirname(destination), { recursive: true });
    await copyFile(await input(file), destination);
  }
  return out;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await buildSite(fileURLToPath(new URL('../', import.meta.url)));
  console.log('Static site ready in dist/ (aggregate data only).');
}
