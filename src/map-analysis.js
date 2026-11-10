const DEFAULT_BAND_COLORS = Object.freeze(['#247e80', '#58a69a', '#d1ac55', '#c87c4a', '#a95253']);
const NO_ROUTE_COLOR = '#87949b';

function thresholdMetres(thresholdKm) {
  if (typeof thresholdKm !== 'number' || !Number.isFinite(thresholdKm) || thresholdKm < 0 || thresholdKm > 400) {
    throw new RangeError('Threshold must be a finite number from 0 to 400 kilometres.');
  }
  return thresholdKm * 1000;
}

const count = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0;

function routeStatus(route, thresholdM) {
  if (route?.status !== 'ok' || typeof route.distanceM !== 'number' || !Number.isFinite(route.distanceM) || route.distanceM < 0) {
    return 'no-route';
  }
  return route.distanceM <= thresholdM ? 'within' : 'beyond';
}

/** Compare supplied road distances only; the threshold itself is inclusive. */
export function thresholdStatus(route, thresholdKm) {
  return routeStatus(route, thresholdMetres(thresholdKm));
}

/** Pass the full assignments array to keep these totals independent of display filters. */
export function summarizeThreshold(assignments, thresholdKm) {
  const thresholdM = thresholdMetres(thresholdKm);
  const summary = {
    within: { members: 0, suburbs: 0 },
    beyond: { members: 0, suburbs: 0 },
    noRoute: { members: 0, suburbs: 0 },
    totalMembers: 0,
  };
  for (const assignment of assignments) {
    const members = count(assignment.members);
    const status = routeStatus(assignment.route, thresholdM);
    const bucket = summary[status === 'no-route' ? 'noRoute' : status];
    bucket.members += members;
    bucket.suburbs += members > 0 ? 1 : 0;
    summary.totalMembers += members;
  }
  return summary;
}

/** Copy only map-facing properties; one positive-member centroid remains one point. */
export function prepareMapFeatures(assignments, { thresholdKm, bandColors = DEFAULT_BAND_COLORS } = {}) {
  const thresholdM = thresholdMetres(thresholdKm);
  const colors = Array.isArray(bandColors) && bandColors.length ? bandColors : DEFAULT_BAND_COLORS;
  const features = assignments.filter((assignment) => count(assignment.members) > 0).map((assignment) => {
    const { feature } = assignment;
    const props = feature.properties ?? {};
    const match = /^band-(0|[1-9]\d*)$/.exec(assignment.bandId);
    const index = match ? Number(match[1]) : -1;
    const bandColor = index >= 0 && Number.isSafeInteger(index) ? colors[index % colors.length] : NO_ROUTE_COLOR;
    return {
      type: 'Feature',
      geometry: { type: feature.geometry.type, coordinates: [...feature.geometry.coordinates] },
      properties: {
        id: props.id ?? assignment.suburbId ?? '',
        name: props.name ?? '',
        postcode: props.postcode ?? '',
        members: assignment.members,
        bandColor,
        thresholdStatus: routeStatus(assignment.route, thresholdM),
      },
    };
  });
  return { type: 'FeatureCollection', features };
}

export const clusterProperties = {
  memberTotal: ['+', ['get', 'members']],
  withinMembers: ['+', ['case', ['==', ['get', 'thresholdStatus'], 'within'], ['get', 'members'], 0]],
  beyondMembers: ['+', ['case', ['==', ['get', 'thresholdStatus'], 'beyond'], ['get', 'members'], 0]],
  noRouteMembers: ['+', ['case', ['==', ['get', 'thresholdStatus'], 'no-route'], ['get', 'members'], 0]],
};

/** MapLibre point_count counts suburb centroids; memberTotal is the weighted total. */
export function clusterSummary(properties) {
  return {
    members: count(properties?.memberTotal),
    suburbs: count(properties?.point_count),
    withinMembers: count(properties?.withinMembers),
    beyondMembers: count(properties?.beyondMembers),
    noRouteMembers: count(properties?.noRouteMembers),
  };
}
