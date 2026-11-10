export const DEFAULT_BAND_BOUNDARIES_KM = Object.freeze([25, 50, 100, 200]);

const numberSetting = (value) => {
  if (typeof value !== 'number' && typeof value !== 'string') return NaN;
  return typeof value === 'string' && !value.trim() ? NaN : Number(value);
};

/** Validate settings without silently replacing invalid values. Ranges are [min, max). */
export function normalizeDistanceSettings(settings = {}) {
  const raw = settings.boundariesKm ?? DEFAULT_BAND_BOUNDARIES_KM;
  const values = typeof raw === 'string' ? raw.split(',') : raw;
  const boundariesKm = Array.isArray(values) ? values.map(numberSetting) : [];
  const errors = {};
  if (!boundariesKm.length || boundariesKm.some((n, i) => !Number.isFinite(n) || n <= 0 || (i && n <= boundariesKm[i - 1]))) {
    errors.boundaries = 'Enter positive, finite kilometre boundaries in strictly increasing order, separated by commas.';
  }
  const optional = (value) => value == null || (typeof value === 'string' && !value.trim()) ? null : numberSetting(value);
  const minKm = optional(settings.minKm);
  const maxKm = optional(settings.maxKm);
  if ((minKm !== null && (!Number.isFinite(minKm) || minKm < 0)) || (maxKm !== null && (!Number.isFinite(maxKm) || maxKm < 0))) {
    errors.range = 'Use finite, non-negative kilometre values, or leave a limit blank.';
  } else if (minKm !== null && maxKm !== null && maxKm <= minKm) {
    errors.range = 'The maximum must be greater than the minimum.';
  }
  return { valid: !Object.keys(errors).length, boundariesKm, minKm, maxKm, errors };
}

const own = (object, key) => object && Object.hasOwn(object, key) ? object[key] : undefined;
function routeResult(entry, groupId) {
  if (entry?.status === 'ok' && typeof entry.distanceM === 'number' && Number.isFinite(entry.distanceM) && entry.distanceM >= 0) {
    return { status: 'ok', groupId, distanceM: entry.distanceM, usesFerry: entry.usesFerry === true };
  }
  return {
    status: ['unreachable', 'unsnapped'].includes(entry?.status) ? entry.status : 'uncomputed',
    groupId, distanceM: null, usesFerry: false,
  };
}

/** Select supplied road distances only. Equal nearest distances use stable group IDs. */
export function chooseRoute(suburbId, groups = [], distances = {}, options = {}) {
  const ordered = [...groups].sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  const entries = own(distances, suburbId);
  if (options.mode !== 'nearest') {
    const group = ordered.find((item) => item.id === options.groupId);
    return routeResult(group ? own(entries, group.id) : undefined, group?.id ?? null);
  }
  const candidates = ordered.map((group) => routeResult(own(entries, group.id), group.id));
  let best = null;
  for (const candidate of candidates) {
    if (candidate.status === 'ok' && (!best || candidate.distanceM < best.distanceM)) best = candidate;
  }
  if (best) return { ...best, incomplete: candidates.some((item) => item.status === 'uncomputed') };
  return candidates.find((item) => item.status === 'uncomputed') ?? candidates.find((item) => item.status === 'unsnapped') ?? candidates[0] ?? routeResult(undefined, null);
}

export function createBands(boundariesKm = DEFAULT_BAND_BOUNDARIES_KM) {
  const settings = normalizeDistanceSettings({ boundariesKm });
  if (!settings.valid) throw new RangeError(settings.errors.boundaries);
  return [0, ...settings.boundariesKm].map((minKm, index) => ({
    id: `band-${index}`, minKm, maxKm: settings.boundariesKm[index] ?? null,
    label: settings.boundariesKm[index] === undefined ? `${minKm}+ km` : `${minKm}–<${settings.boundariesKm[index]} km`,
    members: 0, suburbs: 0, percentage: 0,
  }));
}

export function distanceBandIndex(distanceM, boundariesKm = DEFAULT_BAND_BOUNDARIES_KM) {
  if (typeof distanceM !== 'number' || !Number.isFinite(distanceM) || distanceM < 0) return -1;
  const index = boundariesKm.findIndex((upper) => distanceM < upper * 1000);
  return index < 0 ? boundariesKm.length : index;
}

export function aggregateBandCounts(members, analysis = {}, options = {}) {
  const features = Array.isArray(members) ? members : members?.features ?? [];
  const boundaries = options.boundariesKm ?? DEFAULT_BAND_BOUNDARIES_KM;
  const bands = createBands(boundaries);
  const special = (id, label) => ({ id, label, members: 0, suburbs: 0, percentage: 0 });
  const unreachable = special('unreachable', 'Unreachable');
  const unsnapped = special('unsnapped', 'Unsnapped');
  const uncomputed = special('uncomputed', 'Not computed');
  const statuses = { unreachable, unsnapped, uncomputed };
  let memberSum = 0;
  const assignments = features.map((feature) => {
    const props = feature.properties ?? {};
    const count = typeof props.members === 'number' && Number.isFinite(props.members) && props.members >= 0 ? props.members : 0;
    const route = chooseRoute(props.id, analysis.groups, analysis.distances, options);
    const bucket = route.status === 'ok' ? bands[distanceBandIndex(route.distanceM, boundaries)] : statuses[route.status];
    bucket.members += count;
    bucket.suburbs += count > 0 ? 1 : 0;
    memberSum += count;
    return { feature, suburbId: props.id, members: count, bandId: bucket.id, route };
  });
  const denominator = typeof options.joinedMembers === 'number' && Number.isFinite(options.joinedMembers) && options.joinedMembers >= 0 ? options.joinedMembers : memberSum;
  for (const bucket of [...bands, unreachable, unsnapped, uncomputed]) bucket.percentage = denominator > 0 ? bucket.members / denominator * 100 : 0;
  return {
    bands, unreachable, unsnapped, uncomputed, assignments, denominator, denominatorMismatch: denominator !== memberSum,
    totals: { joinedMembers: denominator, memberSum, suburbs: features.length, reachableMembers: bands.reduce((sum, band) => sum + band.members, 0), unreachableMembers: unreachable.members, unsnappedMembers: unsnapped.members, uncomputedMembers: uncomputed.members },
  };
}

/** Display filtering cannot change the base aggregation or depend on map position. */
export function filterAssignments(assignments, { bandId = null, minKm = null, maxKm = null } = {}) {
  return assignments.filter((item) => {
    if (bandId !== null && item.bandId !== bandId) return false;
    if (minKm !== null || maxKm !== null) {
      if (item.route.status !== 'ok') return false;
      if (minKm !== null && item.route.distanceM < minKm * 1000) return false;
      if (maxKm !== null && item.route.distanceM >= maxKm * 1000) return false;
    }
    return true;
  });
}

export function csvCell(value) {
  let text = value == null ? '' : String(value);
  if (/^[\s\uFEFF]*[=+\-@]/u.test(text) || /^[\t\r\n]/u.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

export function buildBandCsv(summary, { mode = 'group', groupName = '' } = {}) {
  const rows = [['Mode', 'Support group', 'Distance band (km)', 'Lower bound (inclusive)', 'Upper bound (exclusive)', 'Members', 'Suburbs', 'Share of joined members (%)', 'Joined member denominator']];
  for (const bucket of [...summary.bands, summary.unreachable, summary.unsnapped, summary.uncomputed]) {
    rows.push([mode === 'nearest' ? 'Nearest group' : 'Selected group', mode === 'nearest' ? 'Nearest per suburb' : groupName, bucket.label, bucket.minKm, bucket.maxKm, bucket.members, bucket.suburbs, bucket.percentage.toFixed(2), summary.denominator]);
  }
  return rows.map((row) => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
}
