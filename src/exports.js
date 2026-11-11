import { csvCell } from './analysis.js';
import { thresholdStatus } from './map-analysis.js';

const header = ['Suburb/Locality', 'Postcode', 'Members', 'Road distance (km)', 'Support group', 'Analysis mode', 'Distance threshold (km)'];
const compare = new Intl.Collator('en-AU').compare;

/** Export positive-member suburbs using the routes from the active analysis. */
export function buildSuburbCsv(assignments, { thresholdKm, category, mode, groups }) {
  if (category !== 'within' && category !== 'beyond') {
    throw new RangeError('Category must be "within" or "beyond".');
  }
  const groupNames = new Map(groups.map((group) => [group.id, group.name]));
  const selected = assignments
    .filter((assignment) => assignment.members > 0 && thresholdStatus(assignment.route, thresholdKm) === category)
    .sort((a, b) => compare(a.feature.properties.name, b.feature.properties.name)
      || compare(a.feature.properties.postcode, b.feature.properties.postcode)
      || compare(a.suburbId, b.suburbId));
  const rows = [header];
  for (const { feature, members, route } of selected) {
    const source = feature.properties;
    rows.push([source.name, source.postcode, members, route.distanceM / 1000,
      groupNames.get(route.groupId), mode === 'nearest' ? 'Nearest group' : 'Selected group', thresholdKm]);
  }
  return rows.map((row) => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
}
