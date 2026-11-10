import { clusterProperties } from './map-analysis.js';

export const ROAD_ATTRIBUTION = 'Road data © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>';
export const BASEMAPS = Object.freeze({
  osm: { label: 'OpenStreetMap', tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], maxzoom: 19,
    attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>' },
  topo: { label: 'Esri World Topography', tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}'], maxzoom: 23,
    attribution: '<a href="https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer" target="_blank" rel="noopener">Esri World Topography</a> · Sources: Esri, HERE, Garmin, Intermap, increment P Corp., GEBCO, USGS, FAO, NPS, NRCAN, GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), © OpenStreetMap contributors, and the GIS User Community' },
  aerial: { label: 'Esri Aerial', tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'], maxzoom: 23,
    attribution: '<a href="https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer" target="_blank" rel="noopener">Esri World Imagery</a> · Sources: Esri, Vantor, Earthstar Geographics, and the GIS User Community' },
});
export const PALETTES = Object.freeze({
  blue: { label: 'Electric Blue', point: '#006BFF', light: '#8FBDFF', dark: '#003EA6' },
  magenta: { label: 'Magenta', point: '#F00078', light: '#FF9CCD', dark: '#A30052' },
  violet: { label: 'Vivid Violet', point: '#7A24FF', light: '#C5A3FF', dark: '#4A139F' },
  orange: { label: 'Bright Orange', point: '#FF6500', light: '#FFBC8F', dark: '#A63D00' },
});
export const FONT_STACK = 'Noto Sans Regular';
const DENSITY_COLOR = '#247e80';
const POINT_RADIUS = ['interpolate', ['linear'], ['get', 'members'], 1, 6, 10, 8, 50, 12, 150, 16];
const DENSITY_POINT_RADIUS = ['interpolate', ['linear'], ['get', 'members'], 1, 3, 10, 5, 50, 10, 150, 14];
const none = () => ({ type: 'FeatureCollection', features: [] });

export function initialStyle(baseUrl) {
  return { version: 8,
    // URL() percent-encodes braces, but MapLibre needs literal template tokens.
    glyphs: new URL('./public/vendor/fonts/', baseUrl).href + '{fontstack}/{range}.pbf',
    sources: Object.fromEntries(Object.entries(BASEMAPS).map(([id, source]) => [`basemap-${id}`, { type: 'raster', tileSize: 256,
      tiles: source.tiles, maxzoom: source.maxzoom, attribution: source.attribution }])),
    layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#e5eceb' } },
      ...Object.keys(BASEMAPS).map(id => ({ id: `basemap-${id}`, type: 'raster', source: `basemap-${id}`,
        layout: { visibility: id === 'osm' ? 'visible' : 'none' }, paint: { 'raster-fade-duration': 0 } }))],
  };
}

export function addAnalysisLayers(map) {
  for (const id of ['members', 'route', 'selection']) map.addSource(id, { type: 'geojson', data: none() });
  map.addSource('member-clusters', { type: 'geojson', data: none(), cluster: true, clusterRadius: 50,
    clusterMaxZoom: 13, clusterProperties });
  map.addLayer({ id: 'member-heat', type: 'heatmap', source: 'members', maxzoom: 14, paint: {
    'heatmap-weight': ['interpolate', ['linear'], ['get', 'members'], 0, 0, 1, .12, 20, .7, 100, 1],
    'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 4, .7, 10, 2],
    'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 4, 12, 10, 35], 'heatmap-opacity': .8,
    'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'], 0, 'rgba(36,126,128,0)', .15, '#dcecc7', .4, '#88cbbb', .7, '#279da5', 1, '#194674'],
  } });
  map.addLayer({ id: 'route-casing', type: 'line', source: 'route', paint: { 'line-color': '#fff', 'line-width': 7, 'line-opacity': .9 } });
  map.addLayer({ id: 'route-line', type: 'line', source: 'route', paint: { 'line-color': '#1769bb', 'line-width': 4 } });
  const pointPaint = {
    'circle-radius': POINT_RADIUS,
    'circle-color': PALETTES.blue.point,
    'circle-opacity': 1,
    'circle-stroke-color': '#ffffff',
    'circle-stroke-width': 2.5,
  };
  const thresholdFilter = ['in', ['get', 'thresholdStatus'], ['literal', ['beyond', 'no-route']]];
  const thresholdPaint = {
    'circle-radius': ['+', POINT_RADIUS, 4],
    'circle-color': 'rgba(0,0,0,0)',
    'circle-stroke-color': ['match', ['get', 'thresholdStatus'], 'beyond', '#FF6500', '#6e7780'],
    'circle-stroke-width': 2,
  };
  map.addLayer({ id: 'member-threshold-rings', type: 'circle', source: 'members', filter: thresholdFilter,
    paint: thresholdPaint });
  map.addLayer({ id: 'member-points', type: 'circle', source: 'members', paint: pointPaint });
  map.addLayer({ id: 'cluster-threshold-rings', type: 'circle', source: 'member-clusters',
    filter: ['all', ['!', ['has', 'point_count']], thresholdFilter],
    layout: { visibility: 'none' }, paint: { ...thresholdPaint } });
  map.addLayer({ id: 'cluster-points', type: 'circle', source: 'member-clusters', filter: ['!', ['has', 'point_count']],
    layout: { visibility: 'none' }, paint: { ...pointPaint } });
  map.addLayer({ id: 'member-cluster-circles', type: 'circle', source: 'member-clusters', filter: ['has', 'point_count'],
    layout: { visibility: 'none' }, paint: {
      'circle-radius': ['interpolate', ['linear'], ['get', 'memberTotal'], 2, 14, 100, 23, 1000, 35],
      'circle-color': PALETTES.blue.point, 'circle-stroke-color': '#fff', 'circle-stroke-width': 2,
    } });
  map.addLayer({ id: 'cluster-count-labels', type: 'symbol', source: 'member-clusters', filter: ['has', 'point_count'],
    layout: { visibility: 'none', 'text-field': ['to-string', ['get', 'memberTotal']], 'text-font': [FONT_STACK],
      'text-size': 13, 'text-allow-overlap': true, 'text-ignore-placement': true },
    paint: { 'text-color': '#fff', 'text-halo-color': '#173346', 'text-halo-width': 1 },
  });
  const labelLayout = { visibility: 'none', 'text-field': ['get', 'name'], 'text-font': [FONT_STACK], 'text-size': 12,
    'text-anchor': 'top', 'text-offset': [0, 1], 'text-padding': 4,
    'symbol-sort-key': ['-', 0, ['get', 'members']], 'text-allow-overlap': false };
  const labelPaint = { 'text-color': '#183346', 'text-halo-color': '#fff', 'text-halo-width': 1.8 };
  map.addLayer({ id: 'suburb-labels', type: 'symbol', source: 'members', layout: labelLayout, paint: labelPaint });
  map.addLayer({ id: 'cluster-suburb-labels', type: 'symbol', source: 'member-clusters', filter: ['!', ['has', 'point_count']],
    layout: { ...labelLayout }, paint: { ...labelPaint } });
  map.addLayer({ id: 'selected-point', type: 'circle', source: 'selection', paint: {
    'circle-radius': 9, 'circle-color': '#fff', 'circle-stroke-color': '#1769bb', 'circle-stroke-width': 3,
  } });
}

export function applyAppearance(map, settings) {
  const clustered = settings.mapMode === 'clusters';
  const density = settings.mapMode === 'density';
  const visible = (id, show) => map.setLayoutProperty(id, 'visibility', show ? 'visible' : 'none');
  for (const id of Object.keys(BASEMAPS)) visible(`basemap-${id}`, settings.basemap === id);
  const palette = PALETTES[settings.palette] ?? PALETTES.blue;
  visible('member-heat', settings.showMembers && density);
  visible('member-threshold-rings', settings.showMembers && !clustered);
  visible('member-points', settings.showMembers && !clustered);
  visible('cluster-threshold-rings', settings.showMembers && clustered);
  visible('cluster-points', settings.showMembers && clustered);
  visible('member-cluster-circles', settings.showMembers && clustered);
  visible('cluster-count-labels', settings.showMembers && clustered && settings.clusterLabels);
  visible('suburb-labels', settings.showMembers && !clustered && settings.suburbLabels);
  visible('cluster-suburb-labels', settings.showMembers && clustered && settings.suburbLabels);
  const pointRadius = density ? DENSITY_POINT_RADIUS : POINT_RADIUS;
  map.setPaintProperty('member-points', 'circle-radius', pointRadius);
  map.setPaintProperty('member-threshold-rings', 'circle-radius', ['+', pointRadius, 4]);
  map.setPaintProperty('member-points', 'circle-opacity', density ? .45 : 1);
  map.setPaintProperty('member-points', 'circle-color', settings.mapMode === 'bands' ? ['get', 'bandColor']
    : density ? DENSITY_COLOR : palette.point);
  map.setPaintProperty('cluster-points', 'circle-color', palette.point);
  map.setPaintProperty('member-cluster-circles', 'circle-color', ['interpolate', ['linear'], ['get', 'memberTotal'],
    2, palette.light, 100, palette.point, 1000, palette.dark]);
  for (const id of ['route-line', 'route-casing']) visible(id, settings.showRoute);
}
