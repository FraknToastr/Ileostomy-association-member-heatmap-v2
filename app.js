import { aggregateBandCounts, filterAssignments, normalizeDistanceSettings, buildBandCsv, csvCell } from './src/analysis.js';
import { thresholdStatus, summarizeThreshold, prepareMapFeatures } from './src/map-analysis.js';
import { BASEMAPS, PALETTES, ROAD_ATTRIBUTION, initialStyle, addAnalysisLayers, applyAppearance } from './src/map-style.js';

const $ = id => document.getElementById(id);
const number = value => Number(value || 0).toLocaleString('en-AU');
const colours = ['#247e80', '#58a69a', '#d1ac55', '#c87c4a', '#a95253'];
const emptyGeojson = () => ({ type: 'FeatureCollection', features: [] });
const state = {
  mode: 'nearest', groupId: null, boundariesKm: [25, 50, 100, 200],
  bandId: null, minKm: null, maxKm: null, mapMode: 'density', selected: null,
  members: null, analysis: null, report: null, summary: null,
  map: null, mapReady: false, dataSerial: 0, routeSerial: 0,
  routeCache: new Map(), activeRoute: null, routeViewFitted: false,
  selectionView: null, selectionRouteFitted: false,
  showMembers: true, showGroups: true, showRoute: true,
  thresholdKm: 50, basemap: 'osm', palette: 'blue', suburbLabels: false, clusterLabels: true, groupLabels: true,
  mapVersion: 0, featureSignature: null,
  failedBasemaps: new Set(),
};
let dataAbort, routeAbort;

function text(id, value) { $(id).textContent = value; }
function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content != null) node.textContent = content;
  if (className) node.className = className;
  return node;
}
async function fetchData(name, signal) {
  const response = await fetch(new URL(`./public/data/${name}`, document.baseURI), { signal });
  if (!response.ok) throw Error(`Could not load ${name} (HTTP ${response.status}).`);
  return response.json();
}
function options() {
  return { mode: state.mode, groupId: state.groupId, boundariesKm: state.boundariesKm,
    denominator: state.report.totals.joinedMembers };
}
function groupName(id) { return state.analysis.groups.find(g => g.id === id)?.name ?? 'No reachable group'; }
function contextName() { return state.mode === 'nearest' ? 'Nearest reachable support group' : groupName(state.groupId); }
function bucketColour(bucket, i = 0) { return bucket.id.startsWith('band-') ? colours[i % colours.length] : '#87949b'; }
function controlsEnabled(enabled) {
  for (const id of ['analysis-controls', 'range-controls', 'map-display', 'map-settings', 'distance-control', 'suburb-search', 'export-csv', 'clear-filters', 'fit-members']) $(id).disabled = !enabled;
  $('clear-search').disabled = !enabled || !$('suburb-search').value;
  $('fit-route').disabled = !enabled || !state.activeRoute || !state.mapReady;
}

async function loadData() {
  const serial = ++state.dataSerial;
  dataAbort?.abort();
  dataAbort = new AbortController();
  controlsEnabled(false);
  $('data-notice').hidden = false;
  $('retry-data').hidden = true;
  $('data-notice').classList.remove('error');
  text('data-message', 'Loading member counts and road-distance analysis…');
  try {
    const [members, analysis, report] = await Promise.all([
      fetchData('members.geojson', dataAbort.signal), fetchData('analysis.json', dataAbort.signal),
      fetchData('membership-report.json', dataAbort.signal),
    ]);
    if (serial !== state.dataSerial) return;
    if (analysis.schemaVersion !== 1 || !analysis.groups?.length || !members.features?.length) throw Error('The prepared data is incomplete.');
    const sum = members.features.reduce((n, f) => n + f.properties.members, 0);
    if (sum !== report.totals.joinedMembers || sum + report.totals.unresolvedMembers !== report.totals.memberRecords) {
      throw Error('Member totals do not reconcile. Re-run data preparation before using this analysis.');
    }
    Object.assign(state, { members, analysis, report });
    state.groupId = analysis.groups[0].id;
    const select = $('group-select');
    select.replaceChildren(...analysis.groups.map(g => {
      const option = element('option', g.name);
      option.value = g.id;
      return option;
    }));
    select.value = state.groupId;
    text('joined-total', number(sum));
    text('unresolved-total', number(report.totals.unresolvedMembers));
    text('source-date', `Members Oct 2026 · OSM ${analysis.metadata.sourceDate}`);
    $('data-notice').hidden = true;
    text('data-message', '');
    controlsEnabled(true);
    renderQuality();
    renderAnalysis();
    if (!state.map) initializeMap(); else updateMap();
    document.documentElement.dataset.appReady = 'true';
  } catch (error) {
    if (error.name === 'AbortError' || serial !== state.dataSerial) return;
    text('data-message', `${error.message} The analysis is unavailable until the data loads.`);
    $('data-notice').classList.add('error');
    $('retry-data').hidden = false;
  }
}

function renderAnalysis() {
  if (!state.members) return;
  state.summary = aggregateBandCounts(state.members, state.analysis, options());
  renderThreshold();
  $('group-select').disabled = state.mode !== 'group';
  text('support-distance-target', state.mode === 'nearest' ? 'Distance to nearest group' : `Distance to ${contextName()}`);
  text('map-context', contextName());
  text('table-context', contextName());
  text('band-caption', 'Member counts by shortest road distance');
  const rows = [...state.summary.bands, state.summary.unreachable, state.summary.unsnapped, state.summary.uncomputed];
  const body = $('band-rows');
  body.replaceChildren();
  const legend = $('band-legend');
  legend.replaceChildren(element('h3', 'Distance to support'));
  rows.forEach((bucket, i) => {
    if (!bucket.id.startsWith('band-') && !bucket.members && !bucket.suburbs) return;
    const colour = bucketColour(bucket, i);
    const row = element('tr');
    if (state.bandId === bucket.id) row.classList.add('active');
    const heading = element('th');
    heading.scope = 'row';
    const button = element('button', null, 'band-button');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(state.bandId === bucket.id));
    button.setAttribute('aria-label', `${bucket.label}: ${number(bucket.members)} members. Filter map`);
    const label = element('span', null, 'band-label');
    const swatch = element('span', null, 'swatch');
    swatch.style.setProperty('--band-color', colour);
    label.append(swatch, document.createTextNode(bucket.label));
    const track = element('span', null, 'band-track');
    const fill = element('span', null, 'band-fill');
    fill.style.setProperty('--band-color', colour);
    fill.style.width = `${Math.max(0, Math.min(100, bucket.percentage))}%`;
    track.append(fill);
    button.append(label, track);
    button.addEventListener('click', () => { state.bandId = state.bandId === bucket.id ? null : bucket.id; renderAnalysis(); });
    heading.append(button);
    row.append(heading, element('td', number(bucket.members)), element('td', number(bucket.suburbs)), element('td', `${bucket.percentage.toFixed(1)}%`));
    body.append(row);
    if (bucket.id.startsWith('band-')) {
      const key = element('div', null, 'legend-row');
      key.append(swatch.cloneNode(), document.createTextNode(bucket.label));
      legend.append(key);
    }
  });
  text('table-member-total', number(state.summary.totals.memberSum));
  text('table-suburb-total', number(state.members.features.filter(f => f.properties.members > 0).length));
  text('table-share-total', '100.0%');
  text('denominator-note', `Percentages use all ${number(state.summary.denominator)} joined members, including members with no route. Suburb counts include only suburbs with members. Click a band to filter the map.`);
  text('unresolved-note', `${number(state.report.totals.unresolvedMembers)} source members have no matching centroid and are excluded from distance bands. See data quality below.`);
  updateMap();
  if (state.selected) renderSelected();
}

function visibleAssignments() {
  if (!state.summary) return [];
  return filterAssignments(state.summary.assignments, { bandId: state.bandId, minKm: state.minKm, maxKm: state.maxKm });
}
function renderThreshold() {
  if (!state.summary) return;
  const result = summarizeThreshold(state.summary.assignments, state.thresholdKm);
  text('support-distance-value', `${state.thresholdKm} km`);
  $('support-distance').setAttribute('aria-valuetext', `${state.thresholdKm} kilometres by road`);
  text('threshold-heading', `Access at ${state.thresholdKm} km`);
  text('threshold-within-label', `Within ${state.thresholdKm} km`);
  text('threshold-beyond-label', `Beyond ${state.thresholdKm} km`);
  text('threshold-within', number(result.within.members));
  text('threshold-beyond', number(result.beyond.members));
  text('threshold-no-route', number(result.noRoute.members));
  text('beyond-legend-label', `Beyond ${state.thresholdKm} km`);
  if (state.selected) renderSelectedThreshold();
}
function renderSelectedThreshold() {
  const selected = state.summary?.assignments.find(a => a.suburbId === state.selected);
  if (!selected) return;
  const status = thresholdStatus(selected.route, state.thresholdKm);
  text('selected-threshold', status === 'within' ? `Within ${state.thresholdKm} km by road`
    : status === 'beyond' ? `Beyond ${state.thresholdKm} km by road` : 'No computed road route');
  $('selected-threshold').dataset.status = status;
}
function updateMap() {
  const visible = visibleAssignments();
  text('visible-members', number(visible.reduce((n, a) => n + a.members, 0)));
  text('visible-suburbs', `in ${number(visible.filter(a => a.members > 0).length)} suburbs`);
  const label = [...(state.summary?.bands ?? []), state.summary?.unreachable, state.summary?.unsnapped, state.summary?.uncomputed]
    .find(b => b?.id === state.bandId)?.label;
  text('active-band', label ? `Map filter: ${label}` : 'All distance bands');
  $('density-legend').hidden = state.mapMode !== 'density';
  $('band-legend').hidden = state.mapMode !== 'bands';
  $('symbol-legend').hidden = !['points', 'clusters'].includes(state.mapMode);
  $('cluster-legend-note').hidden = state.mapMode !== 'clusters';
  $('threshold-legend').hidden = state.mapMode === 'clusters';
  $('symbol-palette').disabled = !['points', 'clusters'].includes(state.mapMode);
  text('symbol-legend-title', state.mapMode === 'clusters' ? 'Members per cluster' : 'Members per suburb');
  $('symbol-legend-swatch').style.backgroundColor = PALETTES[state.palette].point;
  text('symbol-legend-color', PALETTES[state.palette].label);
  if (!state.mapReady) return;
  const collection = prepareMapFeatures(visible, { thresholdKm: state.thresholdKm, bandColors: colours });
  const signature = JSON.stringify(collection);
  if (signature !== state.featureSignature) {
    state.mapVersion++;
    state.map.getSource('members').setData(collection);
    state.map.getSource('member-clusters').setData(collection);
    state.featureSignature = signature;
  }
  applyAppearance(state.map, state);
  document.querySelectorAll('.group-marker').forEach(marker => {
    marker.hidden = !state.showGroups;
    marker.querySelector('.group-name').hidden = !state.groupLabels;
  });
  if (state.selected) {
    const selected = state.members.features.find(f => f.properties.id === state.selected);
    state.map.getSource('selection').setData({ type: 'FeatureCollection', features: selected ? [selected] : [] });
  }
}

function mapNotice(message) {
  $('map-notice').hidden = !message;
  text('map-message', message || '');
}
function showBasemapNotice() {
  mapNotice(state.failedBasemaps.has(state.basemap)
    ? `${BASEMAPS[state.basemap].label} tiles are unavailable. Choose another basemap in the Map menu. Suburbs, routes and member totals remain available.`
    : '');
}
async function zoomCluster(event) {
  const feature = event.features[0];
  const version = state.mapVersion;
  const map = state.map;
  try {
    const zoom = await map.getSource('member-clusters').getClusterExpansionZoom(feature.properties.cluster_id);
    if (version !== state.mapVersion || map !== state.map || state.mapMode !== 'clusters') return;
    state.routeViewFitted = false;
    map.easeTo({ center: feature.geometry.coordinates, zoom,
      duration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 350 });
  } catch {
    // A source refresh can expire the cluster ID before its expansion is ready.
  }
}
function initializeMap() {
  if (!window.maplibregl) {
    mapNotice('The map library could not load. Member analysis remains available.');
    $('retry-map').hidden = false;
    return;
  }
  try {
    state.map?.remove();
    state.mapReady = false;
    state.featureSignature = null;
    state.failedBasemaps.clear();
    state.map = new maplibregl.Map({
      container: 'map', center: [136.8, -32.8], zoom: 5,
      style: initialStyle(document.baseURI),
      attributionControl: false,
    });
    const map = state.map;
    map.on('movestart', event => { if (event.originalEvent) state.routeViewFitted = false; });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new maplibregl.AttributionControl({ compact: true,
      customAttribution: [ROAD_ATTRIBUTION, '<a href="https://maplibre.org/" target="_blank" rel="noopener">MapLibre</a>'] }), 'bottom-left');
    const attribution = map.getContainer().querySelector('.maplibregl-ctrl-attrib');
    attribution.classList.remove('maplibregl-compact-show');
    attribution.removeAttribute('open');
    map.on('error', event => {
      if (event.sourceId?.startsWith('basemap-')) {
        state.failedBasemaps.add(event.sourceId.slice('basemap-'.length));
        if (event.sourceId === `basemap-${state.basemap}`) showBasemapNotice();
      } else mapNotice('Part of the map could not render. The member analysis remains available.');
    });
    // Analytical overlays must not wait for third-party background tiles.
    map.once('style.load', () => {
      if (map !== state.map) return;
      addAnalysisLayers(map);
      for (const layer of ['member-points', 'cluster-points']) map.on('click', layer, e => selectSuburb(e.features[0].properties.id));
      map.on('click', 'member-cluster-circles', zoomCluster);
      for (const layer of ['member-points', 'cluster-points', 'member-cluster-circles']) {
        map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
      }
      for (const group of state.analysis.groups) {
        const marker = element('button', null, 'group-marker');
        marker.type = 'button';
        marker.append(element('span', null, 'group-dot'), element('span', group.name, 'group-name'));
        marker.addEventListener('click', () => {
          state.mode = 'group'; state.groupId = group.id; state.bandId = null;
          document.querySelector('input[name="analysis-mode"][value="group"]').checked = true;
          $('group-select').value = group.id;
          renderAnalysis();
        });
        new maplibregl.Marker({ element: marker }).setLngLat(group.coordinates).addTo(map);
        // addTo sets a generic label; restore the group's accessible name afterwards.
        marker.setAttribute('aria-label', `${group.name} support group. Analyse this group`);
      }
      state.mapReady = true;
      showBasemapNotice();
      setMapTheme();
      updateMap();
      fitMembers();
      if (state.selected) renderSelected({ fit: true });
    });
  } catch (error) {
    mapNotice('This browser could not start the map. Member tables and CSV export remain available.');
    $('retry-map').hidden = false;
  }
}

function fitMembers() {
  if (!state.mapReady) return;
  state.routeViewFitted = false;
  const features = state.members.features.filter(f => f.properties.members > 0);
  const bounds = new maplibregl.LngLatBounds();
  for (const f of features) bounds.extend(f.geometry.coordinates);
  if (!bounds.isEmpty()) state.map.fitBounds(bounds, { padding: { top: 100, bottom: 70, left: 45, right: 65 }, duration: 0, maxZoom: 8 });
}
function updateSearch() {
  const query = $('suburb-search').value.trim().toLocaleLowerCase('en-AU');
  $('clear-search').disabled = !query;
  const panel = $('search-panel');
  const results = $('search-results');
  panel.hidden = !query;
  if (!query || !state.members) return;
  const matches = state.members.features.filter(f => `${f.properties.name} ${f.properties.postcode}`.toLocaleLowerCase('en-AU').includes(query)).slice(0, 50);
  results.replaceChildren(...matches.map(f => {
    const option = element('option', `${f.properties.name} · ${f.properties.postcode} · ${number(f.properties.members)} members`);
    option.value = f.properties.id;
    return option;
  }));
  results.selectedIndex = -1;
  results.hidden = !matches.length;
  text('search-status', matches.length ? `${matches.length} matching suburbs. Choose a result to view its route.` : 'No matching suburb. Check spelling or review the unmatched names below.');
}
function selectSuburb(id) {
  const feature = state.members.features.find(f => f.properties.id === id);
  if (!feature) return;
  if (!state.selectionRouteFitted || !state.selectionView) {
    state.selectionView = state.mapReady && ['density', 'points', 'bands'].includes(state.mapMode) ? {
      center: state.map.getCenter().toArray(), zoom: state.map.getZoom(),
      bearing: state.map.getBearing(), pitch: state.map.getPitch(), padding: { ...state.map.getPadding() },
    } : null;
  }
  state.selected = id;
  $('selected-suburb').scrollTop = 0;
  $('search-panel').hidden = true;
  $('suburb-search').value = feature.properties.name;
  $('clear-search').disabled = false;
  updateMap();
  renderSelected({ fit: true });
}
function clearRoute() {
  state.activeRoute = null;
  state.routeViewFitted = false;
  $('fit-route').disabled = true;
  state.map?.getSource('route')?.setData(emptyGeojson());
}
function routeCoordinates(line) {
  const geometry = line?.geometry;
  const coordinates = geometry?.type === 'LineString' ? geometry.coordinates
    : geometry?.type === 'MultiLineString' ? geometry.coordinates.flat() : [];
  return coordinates.filter(point => point.length >= 2 && point.every(Number.isFinite));
}
function fitRoute({ animate = true } = {}) {
  if (!state.mapReady || !state.activeRoute) return;
  const { line, suburbId, groupId } = state.activeRoute;
  const coordinates = routeCoordinates(line);
  if (!coordinates.length) return;
  const bounds = new maplibregl.LngLatBounds();
  coordinates.forEach(point => bounds.extend(point));
  const suburb = state.members.features.find(f => f.properties.id === suburbId);
  const group = state.analysis.groups.find(g => g.id === groupId);
  if (suburb) bounds.extend(suburb.geometry.coordinates);
  if (group) bounds.extend(group.coordinates);
  const frame = $('map').getBoundingClientRect();
  const heading = document.querySelector('.map-heading').getBoundingClientRect();
  const actions = document.querySelector('.map-actions').getBoundingClientRect();
  const padding = {
    top: Math.min(frame.height * .35, Math.max(heading.bottom, actions.bottom) - frame.top + 14),
    bottom: Math.min(34, frame.height * .15),
    left: Math.min(28, frame.width * .08),
    right: Math.min(54, frame.width * .15),
  };
  const card = $('selected-suburb');
  if (!card.hidden && card.parentElement.classList.contains('map-stage')) {
    padding.left = Math.min(card.getBoundingClientRect().right - frame.left + 16, frame.width * .58);
  }
  state.showRoute = true;
  $('route-layer-toggle').checked = true;
  applyAppearance(state.map, state);
  state.routeViewFitted = true;
  state.selectionRouteFitted = true;
  state.map.fitBounds(bounds, { padding, maxZoom: 17, bearing: 0, pitch: 0,
    retainPadding: false,
    duration: animate && !matchMedia('(prefers-reduced-motion: reduce)').matches ? 400 : 0 });
}
function placeSelectedCard() {
  const card = $('selected-suburb');
  const mobile = matchMedia('(max-width: 850px)').matches;
  const parent = mobile ? $('selected-suburb-slot') : document.querySelector('.map-stage');
  if (card.parentElement !== parent) parent.append(card);
  card.classList.toggle('in-analysis', mobile);
}
async function renderSelected({ fit = false } = {}) {
  const assignment = state.summary.assignments.find(a => a.suburbId === state.selected);
  if (!assignment) return;
  const { feature, route } = assignment;
  const props = feature.properties;
  const card = $('selected-suburb');
  const wasHidden = card.hidden;
  card.hidden = false;
  placeSelectedCard();
  document.querySelector('.map-stage').classList.add('has-selection');
  text('selected-name', props.name);
  text('selected-members', number(props.members));
  text('selected-distance', route.status === 'ok' ? `${(route.distanceM / 1000).toFixed(1)} km` : 'No route');
  text('selected-group', route.status === 'ok' && route.groupId ? groupName(route.groupId) : 'No reachable support group');
  renderSelectedThreshold();
  if (card.classList.contains('in-analysis') && (fit || wasHidden)) {
    const panel = $('analysis-panel');
    panel.scrollTop += card.getBoundingClientRect().top - panel.getBoundingClientRect().top - 8;
  }
  const key = `${props.id}:${route.groupId}`;
  if (route.status === 'ok' && state.activeRoute?.key === key) {
    state.map?.getSource('route')?.setData({ type: 'FeatureCollection', features: [state.activeRoute.line] });
    $('fit-route').disabled = !state.mapReady;
    if (fit) fitRoute();
    return;
  }
  const serial = ++state.routeSerial;
  routeAbort?.abort();
  clearRoute();
  text('route-status', route.status === 'ok' ? 'Loading road route…' : 'No computed road route.');
  if (route.status !== 'ok') return;
  try {
    let collection = state.routeCache.get(route.groupId);
    if (!collection) {
      routeAbort = new AbortController();
      collection = await fetchData(`routes/${encodeURIComponent(route.groupId)}.geojson`, routeAbort.signal);
      if (serial !== state.routeSerial) return;
      state.routeCache.set(route.groupId, collection);
      while (state.routeCache.size > 2) state.routeCache.delete(state.routeCache.keys().next().value);
    }
    if (serial !== state.routeSerial) return;
    const line = collection.features.find(f => f.properties.suburbId === props.id);
    if (!routeCoordinates(line).length) throw Error('Route geometry is missing.');
    state.activeRoute = { key, line, suburbId: props.id, groupId: route.groupId };
    state.map?.getSource('route')?.setData({ type: 'FeatureCollection', features: [line] });
    $('fit-route').disabled = !state.mapReady;
    text('route-status', `Road route ready · ${groupName(route.groupId)}`);
    fitRoute();
  } catch (error) {
    if (error.name === 'AbortError' || serial !== state.routeSerial) return;
    text('route-status', 'The road route could not load. Select the suburb again to retry.');
  }
}
function renderQuality() {
  const { totals, unresolved } = state.report;
  text('quality-count', `${number(totals.unresolvedMembers)} members to review`);
  const dl = $('quality-totals');
  dl.replaceChildren();
  for (const [label, value] of [['Source members', totals.memberRecords], ['Joined members', totals.joinedMembers],
    ['Unmatched members', totals.unmatchedMembers], ['Ambiguous members', totals.ambiguousMembers], ['Blank rows', totals.blankRows]]) {
    dl.append(element('dt', label), element('dd', number(value)));
  }
  $('unresolved-list').replaceChildren(...unresolved.map(r => element('li', `${r.name} — ${number(r.count)} ${r.count === 1 ? 'member' : 'members'} (${r.reason})`)));
  const meta = state.analysis.metadata;
  const warnings = $('quality-warnings');
  warnings.replaceChildren();
  for (const copy of [meta.boundaryWarning,
    `${number(meta.restrictionWarnings?.length)} conditional or unsupported turn restrictions and ${number(meta.extraction?.unsupportedTags?.length)} road attributes need review. Static routing does not evaluate time-dependent access; results should be reviewed before operational decisions.`,
    `${number(meta.snapReviewCount)} point attachments exceed 1 km or could not be made within 5 km. Attachments are to the nearest eligible road, not necessarily a connected component.`,
    'Every member in a suburb uses its centroid distance. These are not individual home-to-meeting distances. Car routes use conservative public-road access rules and may include ferries.',
  ]) if (copy) warnings.append(element('p', copy));
  text('network-meta', `${number(meta.network.nodes)} road nodes · ${number(meta.network.edges)} directed edges · distance in metres · EPSG:8059. Road geometry is simplified for display only.`);
  text('generated-at', `Prepared ${new Date(meta.generatedAt).toLocaleDateString('en-AU')}. Member snapshot: October 2026.`);
}

function exportCsv() {
  let csv = buildBandCsv(state.summary, { mode: state.mode, groupName: contextName() });
  csv += '\r\n' + [['Source reconciliation', 'Members'], ['Source records', state.report.totals.memberRecords],
    ['Joined members', state.report.totals.joinedMembers], ['Unresolved members (excluded from bands)', state.report.totals.unresolvedMembers]]
    .map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
  const url = URL.createObjectURL(new Blob(['\uFEFF', csv], { type: 'text/csv;charset=utf-8' }));
  const link = element('a');
  link.href = url; link.download = `member-distance-bands-${state.mode === 'nearest' ? 'nearest' : state.groupId}.csv`;
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  text('action-status', 'Distance-band summary exported. Map filters do not alter the exported band totals.');
}
function setMapTheme() {
  if (state.mapReady) for (const id of Object.keys(BASEMAPS)) {
    state.map.setPaintProperty(`basemap-${id}`, 'raster-brightness-max', id !== 'aerial' && document.documentElement.dataset.theme === 'dark' ? .65 : 1);
  }
}
function setTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('theme-toggle').setAttribute('aria-pressed', String(dark));
  text('theme-toggle', dark ? 'Light theme' : 'Dark theme');
  try { localStorage.setItem('support-heatmap-theme', dark ? 'dark' : 'light'); } catch { /* Optional preference storage. */ }
  setMapTheme();
}

$('retry-data').addEventListener('click', loadData);
$('retry-map').addEventListener('click', initializeMap);
$('fit-members').addEventListener('click', fitMembers);
$('fit-route').addEventListener('click', () => fitRoute());
$('suburb-search').addEventListener('input', updateSearch);
$('suburb-search').addEventListener('keydown', e => {
  if (e.key === 'Escape') $('search-panel').hidden = true;
  if (e.key === 'ArrowDown' && !$('search-results').hidden && $('search-results').options.length) { e.preventDefault(); $('search-results').focus(); }
  if (e.key === 'Enter' && !$('search-results').hidden && $('search-results').options.length) { e.preventDefault(); selectSuburb($('search-results').options[0].value); }
});
$('search-results').addEventListener('change', () => selectSuburb($('search-results').value));
$('clear-search').addEventListener('click', () => { $('suburb-search').value = ''; updateSearch(); $('suburb-search').focus(); });
$('close-selection').addEventListener('click', () => {
  const view = state.selectionRouteFitted ? state.selectionView : null;
  state.selectionView = null;
  state.selectionRouteFitted = false;
  state.selected = null; state.routeSerial++; routeAbort?.abort();
  $('selected-suburb').hidden = true;
  document.querySelector('.map-stage').classList.remove('has-selection');
  clearRoute();
  state.map?.getSource('selection')?.setData(emptyGeojson());
  if (view && state.mapReady) state.map.easeTo({ ...view,
    duration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 400 });
});
document.querySelectorAll('input[name="analysis-mode"]').forEach(input => input.addEventListener('change', () => { state.mode = input.value; state.bandId = null; renderAnalysis(); }));
$('group-select').addEventListener('change', () => { state.groupId = $('group-select').value; state.bandId = null; renderAnalysis(); });
document.querySelectorAll('input[name="map-mode"]').forEach(input => input.addEventListener('change', () => {
  state.mapMode = input.value;
  updateMap();
}));
$('support-distance').addEventListener('input', () => {
  state.thresholdKm = Number($('support-distance').value);
  renderThreshold();
  updateMap();
});
$('basemap-select').addEventListener('change', () => {
  state.basemap = $('basemap-select').value;
  updateMap();
  showBasemapNotice();
});
$('symbol-palette').addEventListener('change', () => { state.palette = $('symbol-palette').value; updateMap(); });
for (const [id, key] of [
  ['suburb-label-toggle', 'suburbLabels'], ['cluster-label-toggle', 'clusterLabels'], ['group-label-toggle', 'groupLabels'],
  ['member-layer-toggle', 'showMembers'], ['group-layer-toggle', 'showGroups'], ['route-layer-toggle', 'showRoute'],
]) $(id).addEventListener('change', () => { state[key] = $(id).checked; updateMap(); });

function positionHeaderMenu(panel, button) {
  const anchor = button.getBoundingClientRect();
  const width = Math.min(370, window.innerWidth - 24);
  const top = anchor.bottom + 8;
  panel.style.width = `${width}px`;
  panel.style.left = `${Math.max(12, Math.min(anchor.right - width, window.innerWidth - width - 12))}px`;
  panel.style.top = `${top}px`;
  panel.style.maxHeight = `${Math.max(80, window.innerHeight - top - 12)}px`;
}
const headerMenus = [
  ['map-controls-panel', 'map-controls-toggle', 'map-controls-close', 'map-controls-heading'],
  ['display-filters-panel', 'display-filters-toggle', 'display-filters-close', 'display-heading'],
];
for (const [panelId, buttonId, closeId, headingId] of headerMenus) {
  const panel = $(panelId), button = $(buttonId);
  button.addEventListener('click', () => positionHeaderMenu(panel, button));
  panel.addEventListener('toggle', event => {
    const open = event.newState === 'open';
    button.setAttribute('aria-expanded', String(open));
    if (open) {
      positionHeaderMenu(panel, button);
      $(headingId).focus({ preventScroll: true });
    } else if (panel.contains(document.activeElement) || document.activeElement === document.body) {
      button.focus({ preventScroll: true });
    }
  });
  $(closeId).addEventListener('click', () => { panel.hidePopover(); button.focus({ preventScroll: true }); });
}
let resizeFrame;
function updateLayout() {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => {
    placeSelectedCard();
    for (const [panelId, buttonId] of headerMenus) positionHeaderMenu($(panelId), $(buttonId));
    if (state.mapReady) {
      state.map.resize();
      if (state.routeViewFitted) fitRoute({ animate: false });
    }
  });
}
window.addEventListener('resize', updateLayout);
window.visualViewport?.addEventListener('resize', updateLayout);
new ResizeObserver(updateLayout).observe(document.querySelector('.map-stage'));
placeSelectedCard();

$('band-form').addEventListener('submit', e => {
  e.preventDefault();
  const settings = normalizeDistanceSettings({ boundariesKm: $('band-boundaries').value });
  $('band-error').hidden = settings.valid;
  $('band-boundaries').setAttribute('aria-invalid', String(!settings.valid));
  text('band-error', settings.errors.boundaries || '');
  if (!settings.valid) return;
  state.boundariesKm = settings.boundariesKm; state.bandId = null; renderAnalysis();
});
$('range-form').addEventListener('submit', e => {
  e.preventDefault();
  const settings = normalizeDistanceSettings({ boundariesKm: state.boundariesKm, minKm: $('distance-min').value, maxKm: $('distance-max').value });
  $('range-error').hidden = settings.valid;
  text('range-error', settings.errors.range || '');
  if (!settings.valid) return;
  state.minKm = settings.minKm; state.maxKm = settings.maxKm; updateMap();
});
$('clear-filters').addEventListener('click', () => {
  state.bandId = null; state.minKm = null; state.maxKm = null;
  $('distance-min').value = ''; $('distance-max').value = ''; $('range-error').hidden = true; renderAnalysis();
});
$('export-csv').addEventListener('click', exportCsv);
$('theme-toggle').addEventListener('click', () => setTheme(document.documentElement.dataset.theme !== 'dark'));
let dark = false;
try { dark = localStorage.getItem('support-heatmap-theme') === 'dark'; } catch { /* Optional preference storage. */ }
setTheme(dark);
loadData();
