# Ileostomy association membership and support group heatmap

This project prepares anonymous suburb membership totals and driving distances from South Australian suburb points to support groups, then displays the prepared results in a browser. Membership preparation, road extraction and routing run locally. The browser reads the resulting public data; it does not read the membership workbook or calculate routes from the road graph.

## Public GitHub Pages site

Open [the public member heatmap](https://frakntoastr.github.io/Ileostomy-association-member-heatmap-v2/).

Git tracks the application source, tests, configuration and approved prepared data in `public/data/`: current suburb member counts, distance analysis, reconciliation and snap reports, the manifest, and support-group routes. These aggregate files are public and can be downloaded by visitors. Raw workbooks and source GeoJSON inputs, the OSM PBF and routing graph, installed dependencies, environments, build output and test reports stay local. Every PBF file, including font glyphs, is excluded from Git and the website build.

The browser loads Noto Sans Regular glyphs from the [official MapLibre demo font endpoint](https://demotiles.maplibre.org/font/Noto%20Sans%20Regular/0-255.pbf); labels and basemaps require internet access. Tests download three checksummed glyph ranges into an ignored cache and intercept glyph requests for repeatable checks. No PBFs are committed or deployed.

GitHub Pages uses the `Deploy GitHub Pages` Actions workflow, triggered by a push to `main` or a manual dispatch. It installs locked dependencies on Node 22, checks the data and application, builds and tests the site at its repository URL prefix, and publishes only `dist/`. Pages settings must use **GitHub Actions**, not the repository root. A failed build or test does not replace the last successful deployment. Reverting a problematic commit on `main` redeploys the previous source/data version.

Membership and road processing remain local. When inputs change, regenerate the prepared data, review its reconciliation and reports, then commit the updated public data with the matching application changes. The deployment never needs the workbook, raw road PBF or a live routing service.

## Interactive map controls

- Search by **suburb or postcode**, clear the search, and adjust the distance slider on one row. The app fits one screen; the analysis panel scrolls internally. On mobile the map stays above the analysis panel.
- **Distance to Support Group** spans 0–400 km in 10 km steps, initially 50 km. **Nearest group** compares each suburb with its nearest reachable support group. **Selected group** compares every suburb with the chosen group. The slider label identifies its current target. Threshold totals always use all joined members, independently of display filters.
- Open **Map** or **Display Filters** beside **Dark theme**. The menus close with Escape, Close, or an outside click. Display Filters contains the road-distance range, clear action, and visible-member summary.
- Choose **OpenStreetMap**, **Esri World Topography**, or **Esri Aerial**. Switching preserves the camera, selected suburb, route, and analysis. If tiles fail, choose another basemap in Map; local data and overlays remain available.
- Display **Member density**, **Individual points**, **Clusters**, or **Distance bands**. Points and clusters offer **Electric Blue**, **Magenta**, **Vivid Violet**, and **Bright Orange**. Individual points are opaque with white halos; separate orange or gray rings show beyond-threshold or missing-route status. Density and distance bands retain their semantic colors.
- Cluster sizes and count labels use **summed members**. Clicking a cluster zooms in. Clicking a suburb or choosing a search result shows its details in Selected Suburb and fits its complete shortest road route. **Fit Route**, beside **Fit Members**, refits the loaded route after panning or zooming. It is disabled until a route is available. In Member density, Individual points, and Distance bands, closing Selected Suburb restores the map view from before the first route selection. There are no map popups.
- Toggle **Suburb labels** (initially off), **Cluster member counts** (initially on), and **Support Group labels** (initially on) independently. Turning off group labels leaves group dots visible. Font glyphs are loaded from MapLibre’s hosted Noto Sans Regular endpoint.
- All attribution appears in the **MapLibre info icon, collapsed by default**. Credits follow the active basemap and retain OSM road-data and MapLibre attribution.

Basemap service metadata: [Esri World Topography](https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer) and [Esri World Imagery](https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer). These are third-party online tile services; prepared member and route data are published as static assets.

## Current source reconciliation

The October 2026 workbook has one `Residential Suburb` column and 3,105 data rows. Every populated row counts as one member, including repeated suburb values. The workbook contains no blank suburb rows.

| Measure | Prepared result |
| --- | ---: |
| Source rows, excluding the header | 3,105 |
| Member records | 3,105 |
| Members joined by normalized exact name | 3,014 |
| Additional members joined through explicit aliases | 5 |
| Joined members | 3,019 |
| Unresolved members | 86 |
| Unmatched members | 86 |
| Ambiguous members | 0 |
| Blank rows | 0 |
| Distinct unresolved names | 38 |
| Suburb point features retained | 1,895 |
| Suburbs with at least one joined member | 589 |
| Suburbs with zero joined members | 1,306 |
| Source support group points | 11 |
| Joined members with a reachable nearest support group | 3,018 |
| Joined members with no reachable support group | 1 |

The required membership reconciliation is `3,105 = 3,019 + 86`. Including the nearest-group routing result, `3,105 = 3,018 reachable + 1 unreachable + 86 unresolved`. Joined members represent approximately 97.23% of member records. Unresolved members remain in the report and are excluded from mapped suburb membership counts. A suburb with zero joined members can still be retained for geographic analysis. Routing totals describe the prepared dataset; check the regenerated analysis after changing sources or routing rules.

Unresolved examples include **Broken Hill (39)**, **Mildura (4)**, **Merbein (3)** and **Alice Springs (2)**. These include places outside the South Australian suburb dataset. Other unresolved labels can need local review: the pipeline does not assume that every unresolved name is outside the state, or choose a geographically similar name automatically.

## Files and publishing boundary

| File or directory | Purpose |
| --- | --- |
| `Heat Map Data October 2026.xlsx` | Original local membership source; preserve it and keep it private. |
| `Suburbs_and_Localities.geojson` | Source of 1,895 suburb point locations, names, postcodes and `OBJECTID` values. |
| `Ileo_Support_Groups.geojson` | Source of 11 support group points. |
| `south-australia-261108.osm.pbf` | Local OpenStreetMap road snapshot; extracted source timestamp is dated 2026-11-08. |
| `config/suburb-aliases.json` | Reviewed, explicit membership name mappings. |
| `SupportHeatmapNetwork.pyt` | Road extraction implementation used by the preparation workflow. |
| `scripts/extract_network.py` | CLI entry point for extracting the road graph. |
| `.generated/network.json` | Local routing graph; preparation input, not a web asset. |
| `scripts/prepare_members.py` | Standard library XLSX reader, aggregation and suburb join. |
| `scripts/route_distances.py` | Snapping, directed shortest paths and route exports. |
| `public/data/` | Prepared suburb aggregates, routing analysis and review reports. |
| `public/vendor/` | Browser library assets prepared by the vendor command. |
| `dist/` | Built website and the only directory to publish. |

The original workbook, source PBF, local graph, Python environment and extraction tools belong outside the published site. The legacy `UOS_Transport_OSM_Network_SA_Lambert_8059_Fix.pyt` is preserved as a source artifact; the current workflow uses `SupportHeatmapNetwork.pyt`.

Use the project's development server, which allows only application assets, `src/`, `public/data/` and `public/vendor/`. Do not serve the repository root with a general file server: it contains the private workbook and local preparation inputs. Build with `npm run build` and publish **only `dist/`**.

## Setup

A fresh clone already contains the prepared data needed by the browser. Use Node 22 in a Linux or WSL Bash terminal from the project root. Commands use the RTK wrapper required for this workspace.

```bash
rtk npm ci
rtk npm run build
rtk npm run dev
```

The development app is available at `http://127.0.0.1:4173`. To preview exactly the built GitHub Pages bundle, stop the development server if desired and run:

```bash
rtk npm run preview
```

Open `http://127.0.0.1:4174/Ileostomy-association-member-heatmap-v2/`. This server exposes only the built site at its project prefix.

Python is needed only to regenerate data from the original local inputs. The current preparation environment uses Python 3.13:

```bash
rtk proxy python3 -m venv .venv
rtk proxy .venv/bin/python -m pip install -r requirements.txt
```

`requirements.txt` pins the road extraction, projection and spatial-indexing dependencies. Membership preparation uses only Python's standard library. Keep original inputs local in the locations below or pass the CLI path arguments.

## Prepare and run

Prepare the data in dependency order:

```bash
rtk npm run prepare:members
rtk npm run prepare:network
rtk npm run prepare:routes
```

The combined command runs those same stages in order:

```bash
rtk npm run prepare:data
```

Run membership preparation alone when reviewing the workbook or aliases. A change to member counts does not require extracting the PBF again. Route preparation consumes `public/data/members.geojson` and `.generated/network.json`, so produce both before routing.

Routing defaults to four workers. Use one worker on a machine with limited RAM, including a memory constrained Windows/WSL host:

```bash
rtk npm run prepare:routes -- --workers 1
```

To select another local road snapshot or graph path:

```bash
rtk proxy .venv/bin/python scripts/extract_network.py --pbf south-australia-261108.osm.pbf --output .generated/network.json
rtk proxy .venv/bin/python scripts/route_distances.py --graph .generated/network.json --members public/data/members.geojson --groups Ileo_Support_Groups.geojson --output public/data --workers 4
```

After preparation, start the application:

```bash
rtk npm run dev
```

The default development address is `http://127.0.0.1:4173`. Read the server's printed address if a port override is supplied. The application loads prepared distances and route geometry; rerun the relevant preparation stage when an input changes.

Build the publishable application after preparing the real routing data:

```bash
rtk npm run build
```

The build vendors MapLibre JavaScript, CSS and licence, clears stale output, and copies only the application and explicitly allowed prepared data into `dist/`. PBFs, workbooks, raw graphs and development files are never copied. Always publish only `dist/`, keeping relative application and data paths together.

## Membership preparation and alias review

The XLSX reader uses `zipfile` and XML parsing. It handles shared strings, inline strings, rich text and sparse worksheet rows. It reads all sheets whose first populated row has a `Residential Suburb` header, or one named sheet when `--sheet` is supplied. Headers are excluded from `sourceRows`; missing interior rows and blank suburb cells below a matching header count as `blankRows`. An uncalculated suburb formula is rejected rather than silently counted as blank.

Names are normalized with Unicode NFKC normalization, collapsed whitespace and Unicode case folding. Accents are retained. The join uses normalized names and explicitly configured aliases. It does not deduplicate members, perform fuzzy matching, infer a postcode or relocate an unmatched member to a nearby suburb.

Current aliases preserve the original source labels as explicit entries:

| Aggregate original workbook label | Records | Explicit target |
| --- | ---: | --- |
| `MOUNT GAMBIER DC` | 1 | `MOUNT GAMBIER` |
| `MOUNT GAMBIER EAST` | 1 | `MOUNT GAMBIER` |
| `MOUNT GAMBIER WEST` | 2 | `MOUNT GAMBIER` |
| `VICTOR HARBOR CENTRAL` | 1 | `VICTOR HARBOR` |

These four mappings account for five additional joined records. They are reviewable aggregation choices in `config/suburb-aliases.json`; the workbook is not corrected or rewritten.

To review a new label:

1. Find its normalized original name and aggregate count in `public/data/membership-report.json`.
2. Check the intended location against `Suburbs_and_Localities.geojson`. An unresolved out-of-state name can appropriately remain unresolved.
3. Add a verified source-name-to-canonical-name entry to `config/suburb-aliases.json`. For a duplicate canonical name, target an explicit stable ID such as `suburb-858` only after identifying the intended point.
4. Rerun membership preparation and the membership tests. Check the change in joined and unresolved counts against the aggregate count of the label you reviewed.
5. Regenerate dependent analysis and rebuild the site before publishing updated results.

Aliases must target a name or ID present in the suburb source. Unknown targets and conflicting normalized alias keys fail preparation. Multiple matching suburb points remain ambiguous and are reported with candidate IDs, names and postcodes; the first candidate is never chosen automatically. Reports retain unresolved original geographic labels after normalization, rather than silently substituting a guessed location.

Default source paths are resolved relative to the script's project root. Custom inputs and a single sheet can be selected with:

```bash
rtk proxy python3 scripts/prepare_members.py --source "Heat Map Data October 2026.xlsx" --suburbs Suburbs_and_Localities.geojson --aliases config/suburb-aliases.json --sheet Sheet1 --output-dir public/data
```

The script also accepts `--workbook` or `--members` as synonyms for `--source`. It writes only `members.geojson` and `membership-report.json`, protects source paths from being overwritten, and prints the reconciliation totals. Repeated runs with the same inputs produce identical membership output bytes.

### Membership output contract

`members.geojson` retains every source suburb point. Each feature has the stable feature ID `suburb-<OBJECTID>` and exactly these public properties:

| Property | Meaning |
| --- | --- |
| `id` | The same stable ID as the feature ID. |
| `name` | Canonical suburb name from the point source. |
| `postcode` | String preserving leading zeroes. |
| `members` | Integer count of joined workbook rows. |
| `membershipMatch` | `exact`, `alias`, `mixed` when both methods contributed, or `none` when the count is zero. |

`membership-report.json` has `totals` and `unresolved`. Its totals are `sourceRows`, `memberRecords`, `joinedMembers`, `unresolvedMembers`, `blankRows`, `unmatchedMembers` and `ambiguousMembers`. Each unresolved item has `name`, `count` and `reason` (`unmatched` or `ambiguous`); ambiguous items also have `candidates`. Names in this list are normalized aggregate keys, not individual workbook records.

Preparation enforces:

```text
sourceRows = memberRecords + blankRows
memberRecords = joinedMembers + unresolvedMembers
unresolvedMembers = unmatchedMembers + ambiguousMembers
joinedMembers = sum(members across all suburb features)
unresolvedMembers = sum(count across the unresolved list)
```

No per-member rows, original workbook columns or extra source suburb fields are published. Suburb counts and unresolved name counts are aggregates, including counts of one.

## ArcGIS toolbox

Add `SupportHeatmapNetwork.pyt` to ArcGIS Pro. Its extraction tool accepts the local OSM PBF, an output graph path, and a Python executable. It delegates to the same `scripts/extract_network.py` used by the CLI; it does not build an ArcGIS Network Dataset or require Network Analyst.

This machine's Windows ArcGIS Pro **3.7.2**, **ArcInfo** licence, and Python **3.13.13** were verified by importing `arcpy`. That environment has SciPy but lacks `osmium` and `pyproj`. In ArcGIS Pro's Python Package Manager, create a writable clone rather than modifying the default environment, install `osmium==4.3.2` and `pyproj==3.8.0`, and select the clone's Windows `python.exe` in the toolbox. The Python version must support those package versions. A Windows ArcGIS process cannot directly launch this project's Linux `.venv/bin/python`; use the working WSL CLI commands instead when preparing data here.

The `.pyt` delegation and parameter handling have automated tests. The desktop ArcGIS geoprocessing UI was not launched during verification.

## Road routing methodology

The extractor builds a directed road graph from the local OpenStreetMap PBF snapshot, respecting the supported vehicle access, direction and turn restriction rules. The graph's `source.timestamp` is dated **2026-11-08**. Current extraction flags **878 unsupported road rule tags** and **80 conditional restrictions** for review. These flags do not claim that unsupported rules or time dependent conditions have been evaluated.

Suburb and support group points are transformed to **EPSG:8059** for metre based snapping and length calculations. Snapping searches for the nearest road segment, rather than only the nearest existing road vertex. Points more than **5 km** from a usable road are rejected. Accepted snaps more than **1 km** away are flagged for review. Check `snap-report.json` before relying on those assignments.

Distances follow the direction **suburb → support group**. Dijkstra search uses edge state to enforce supported turn restrictions, including via-way restrictions. Directed costs matter: a reverse journey need not have the same distance. Reported costs use road edge lengths; the straight-line offsets from source points to their snapped positions are **excluded**. A missing or unreachable route does not mean a distance of zero.

Route polylines are simplified with a **20 m** tolerance for display. This changes the displayed geometry, not the shortest-path calculation or its recorded cost. The results are shortest road-network distances for the supported model and snapshot, rather than traffic forecasts or travel-time estimates. Roads outside the local snapshot, incomplete OpenStreetMap tags and flagged conditional restrictions can affect coverage or interpretation.

Routing writes these public artifacts:

| Output | Contents |
| --- | --- |
| `public/data/analysis.json` | Support groups, prepared suburb-to-group distances and routing metadata. |
| `public/data/routes/<group-id>.geojson` | Display route geometries for a support group. |
| `public/data/snap-report.json` | Snap assignments and cases requiring review. |
| `public/data/manifest.json` | Generated file inventory and routing provenance/metadata. |

Read the generated reports for current reachability and snapping metrics. Successful membership matching and successful road routing measure different things: joined members can belong to a suburb whose route needs review or is unavailable.

## Basemap and attribution

The map uses OpenStreetMap's standard raster tiles without an API token. Keep visible OpenStreetMap contributor attribution and follow the [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/). The public tile service is intended for modest interactive use, with best-effort availability. Preserve normal browser caching and a valid HTTP Referer. Do not add bulk downloading, tile prefetching or offline tile packages against that service. Token-free access does not provide an offline map or an availability guarantee. Review the policy and arrange a suitable tile service before expanding usage substantially.

## Validation

Run membership tests independently with either system Python or the project environment:

```bash
rtk proxy python3 -B -m unittest discover -s tests -p test_members.py
rtk proxy .venv/bin/python -B -m unittest discover -s tests -p test_members.py
```

All **18 membership tests** pass in both environments. They exercise real ZIP/XML fixtures, repeated rows, Unicode, explicit aliases, ambiguous candidates, sparse blanks, formulas, stable IDs, source preservation, deterministic output and the actual 3,105-record reconciliation.

Run the JavaScript and browser checks using the committed prepared data:

```bash
rtk npm test
rtk npm run build
rtk npm run test:e2e
rtk npm run test:pages
```

Python preparation tests run locally with `rtk npm run test:python` and the original input files. Browser checks use Playwright and Chromium. On a new machine, install the browser runtime before running them:

```bash
rtk npx playwright install chromium
```

Before publishing, inspect unresolved membership names, snap review cases, conditional restriction flags and the generated manifest, then rebuild `dist/`. Keep the source workbook private throughout the workflow.
