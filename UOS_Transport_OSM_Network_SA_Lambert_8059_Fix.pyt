# -*- coding: utf-8 -*-
"""
Bike Direct CKAN GeoJSON -> Sanity Check -> Enrich -> Network-Ready Output
ArcGIS Pro 3.5+ Python Toolbox (.pyt)

Updates in this version:
- 02 tool uses projected-metre length workflow when Target SR is provided
  (default WKID 7854, GDA2020 / MGA Zone 54).
- one_way normalization is explicit: FT/TF/N/2W/22/blank -> DirCode.
- Path-type normalization fields are added:
  Type_Clean, Class_Clean, PathTypeKey = Type_Clean + \" | \" + Class_Clean.
- Optional Included Path Types parameter filters output edges for manual ND testing.

How to run:
1. Add this .pyt toolbox in ArcGIS Pro.
2. Optional: run "01-Fetch-Bike-Direct-Data" to only download the source file.
3. Run "02-BikeDirect CKAN Download Enrich" to create enriched network-ready output.
4. Use output feature class in Network Dataset build workflow.

Assumptions:
- CKAN package_show endpoint is public and available without auth; fallback
  endpoints may be used when primary endpoint returns HTTP 403/404.
- If GeoJSON has no CRS definition, coordinates are assumed WGS84 (EPSG:4326).
- "Network-ready" means a cleaned polyline feature class with bike/e-bike time
  impedance fields and basic access/direction fields suitable for next ND steps.
"""

import arcpy
import csv
import datetime
import json
import os
import re
import struct
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile


def _split_osm_ways_at_shared_nodes(ways, nodes, allowed_highways):
    """Generate routable OSM line parts without inventing spatial junctions.

    Each retained OSM way is split at its endpoints and at shared OSM node IDs.
    This preserves road/path junctions across edge source classes while leaving
    geometrically crossing ways disconnected unless they share an OSM node.
    Missing OSM nodes break the line rather than allowing an invalid shortcut.

    Pure Python: can be tested without an ArcGIS installation.
    """
    from collections import Counter

    retained = [
        way for way in ways
        if way.get("tags", {}).get("highway") in allowed_highways
        and len(way.get("nodes", [])) >= 2
    ]
    node_way_count = Counter()
    for way in retained:
        node_way_count.update(set(way.get("nodes", [])))

    features = []
    missing_node_references = 0
    for way in retained:
        node_ids = way["nodes"]
        tags = way.get("tags", {})
        attrs = {
            "osm_id": str(way.get("id", "")),
            "highway": tags.get("highway"),
            "name": tags.get("name"),
            "maxspeed": tags.get("maxspeed"),
            "oneway": tags.get("oneway"),
            "junction": tags.get("junction"),
            "bridge": tags.get("bridge"),
            "tunnel": tags.get("tunnel"),
            "layer": tags.get("layer"),
            "surface": tags.get("surface"),
            "lanes": tags.get("lanes"),
            "access": tags.get("access"),
            "motor_vehicle": tags.get("motor_vehicle"),
        }
        current = []
        for i, node_id in enumerate(node_ids):
            coord = nodes.get(node_id)
            if coord is None:
                missing_node_references += 1
                current = []
                continue

            # Protect against degenerate zero-length segments.
            if current and current[-1] == coord:
                continue
            current.append(coord)
            is_way_end = i == len(node_ids) - 1
            is_junction = node_way_count[node_id] > 1
            if is_way_end or is_junction:
                if len(current) >= 2:
                    features.append(dict(attrs, geometry=current))
                # Continue from the same junction node for the next segment.
                current = [coord]

    return features, len(retained), missing_node_references


def _timestamp_compact():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_float(value, default_value=None):
    try:
        if value is None:
            return default_value
        return float(value)
    except Exception:
        return default_value


def _bool_value(param):
    if param is None:
        return False
    val = param.value
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in ("true", "1", "yes", "y")


def _geometry_is_empty(geom):
    if geom is None:
        return True
    try:
        return bool(geom.isEmpty)
    except Exception:
        pass
    try:
        return int(geom.pointCount) == 0
    except Exception:
        pass
    try:
        return int(geom.partCount) == 0
    except Exception:
        pass
    return False


def _normalize_name(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").strip().lower())


def _resource_is_geojson(resource):
    fmt = str(resource.get("format", "")).strip().lower()
    url = str(resource.get("url", "")).strip().lower()
    name = str(resource.get("name", "")).strip().lower()
    return (
        ("geojson" in fmt)
        or url.endswith(".geojson")
        or ("geojson" in url)
        or ("geojson" in name)
    )


def _http_get_json(url, retries=3, timeout=60):
    last_error = None
    parsed = urllib.parse.urlparse(url)
    origin = "{}://{}/".format(parsed.scheme, parsed.netloc) if parsed.scheme and parsed.netloc else ""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if origin:
        headers["Referer"] = origin
        headers["Origin"] = origin.rstrip("/")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                code = getattr(response, "status", None)
                if code is not None and int(code) >= 400:
                    raise RuntimeError("HTTP {} for URL {}".format(code, url))
                payload = response.read()
                return json.loads(payload.decode("utf-8"))
        except Exception as ex:
            last_error = ex
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
            else:
                raise RuntimeError("Failed to fetch JSON after {} tries: {}".format(retries, last_error))


def _download_with_retry(url, out_path, retries=3, timeout=120):
    last_error = None
    parsed = urllib.parse.urlparse(url)
    origin = "{}://{}/".format(parsed.scheme, parsed.netloc) if parsed.scheme and parsed.netloc else ""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if origin:
        headers["Referer"] = origin
        headers["Origin"] = origin.rstrip("/")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                code = getattr(response, "status", None)
                if code is not None and int(code) >= 400:
                    raise RuntimeError("HTTP {} for URL {}".format(code, url))
                with open(out_path, "wb") as out_file:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        out_file.write(chunk)
            if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
                raise RuntimeError("Downloaded file is empty: {}".format(out_path))
            return out_path
        except Exception as ex:
            last_error = ex
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
            else:
                raise RuntimeError("Download failed after {} tries: {}".format(retries, last_error))


def _validate_download_signature(path):
    try:
        with open(path, "rb") as fobj:
            first = fobj.read(256)
        trimmed = first.lstrip()
        return bool(trimmed) and (trimmed[:1] in (b"{", b"["))
    except Exception:
        return False


def _build_ckan_package_show_candidates(ckan_base, dataset_slug):
    base = (ckan_base or "").strip().rstrip("/")
    query = urllib.parse.urlencode({"id": dataset_slug})
    candidates = []

    def _add(url):
        if url and url not in candidates:
            candidates.append(url)

    _add("{}/api/3/action/package_show?{}".format(base, query))

    if not re.search(r"/data$", base, flags=re.IGNORECASE):
        _add("{}/data/api/3/action/package_show?{}".format(base, query))

    # data.sa.gov.au can return 403 from some client contexts; mirrored data.gov.au
    # dataset API is a resilient fallback for the same package slug.
    if "data.sa.gov.au" in base.lower():
        _add("https://data.gov.au/data/api/3/action/package_show?{}".format(query))

    return candidates


def _discover_ckan_package(ckan_base, dataset_slug, log_message, log_warning):
    candidates = _build_ckan_package_show_candidates(ckan_base, dataset_slug)
    if not candidates:
        raise RuntimeError("No CKAN package_show candidate URLs were generated.")

    failures = []
    for idx, url in enumerate(candidates, start=1):
        try:
            log_message("CKAN package_show attempt {}/{}: {}".format(idx, len(candidates), url))
            payload = _http_get_json(url, retries=3, timeout=60)
            if idx > 1:
                log_warning(
                    "Primary CKAN endpoint was unavailable; using fallback endpoint: {}".format(url)
                )
            return payload, url, failures
        except Exception as ex:
            msg = "{} -> {}".format(url, ex)
            failures.append(msg)
            log_warning("CKAN endpoint failed: {}".format(msg))

    raise RuntimeError(
        "All CKAN package_show endpoint attempts failed for dataset '{}'. Last error: {}".format(
            dataset_slug,
            failures[-1] if failures else "Unknown",
        )
    )


def _safe_extract_zip(zip_path, extract_folder):
    extracted = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            normalized = os.path.normpath(member).replace("\\", "/")
            if normalized.startswith("../") or normalized.startswith("/"):
                continue
            target_path = os.path.join(extract_folder, normalized)
            target_dir = os.path.dirname(target_path)
            if target_dir and not os.path.isdir(target_dir):
                os.makedirs(target_dir)
            with zf.open(member, "r") as src, open(target_path, "wb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    dst.write(block)
            extracted.append(target_path)
    return extracted


def _prepare_download_for_import(download_path, download_dir, selected_resource_is_geojson, log_message, log_warning):
    if not download_path.lower().endswith(".zip"):
        return download_path

    extract_dir = os.path.join(download_dir, "_extracted_{}".format(_timestamp_compact()))
    if not os.path.isdir(extract_dir):
        os.makedirs(extract_dir)

    extracted_files = _safe_extract_zip(download_path, extract_dir)
    if not extracted_files:
        raise RuntimeError("ZIP download did not contain files to extract: {}".format(download_path))

    geojson_files = [
        p for p in extracted_files if p.lower().endswith(".geojson") or p.lower().endswith(".json")
    ]
    if geojson_files:
        def _rank(path):
            name = os.path.basename(path).lower()
            if "gda2020" in name:
                return (0, name)
            if "gda94" in name:
                return (1, name)
            return (2, name)

        chosen_geojson = sorted(geojson_files, key=_rank)[0]
        log_message("ZIP extracted; using GeoJSON file for import: {}".format(chosen_geojson))
        return chosen_geojson

    if selected_resource_is_geojson:
        raise RuntimeError(
            "Selected resource was GeoJSON but ZIP did not include a .geojson/.json file."
        )

    shp_files = [p for p in extracted_files if p.lower().endswith(".shp")]
    if shp_files:
        chosen_shp = sorted(shp_files)[0]
        log_warning("ZIP had no GeoJSON; using shapefile fallback for import: {}".format(chosen_shp))
        return chosen_shp

    raise RuntimeError(
        "ZIP resource does not contain supported import files (.geojson/.json/.shp)."
    )


def _resolve_output_file_path(output_folder, file_prefix, url_or_ext, if_exists_mode):
    if not os.path.isdir(output_folder):
        raise RuntimeError("Output folder does not exist: {}".format(output_folder))

    prefix = (file_prefix or "bikedirect").strip()
    prefix = re.sub(r"[^0-9A-Za-z_-]", "_", prefix)
    if not prefix:
        prefix = "bikedirect"

    ext = ".dat"
    if url_or_ext:
        raw = str(url_or_ext).strip()
        parsed = urllib.parse.urlparse(raw)
        lower = (parsed.path or raw).lower().strip()
        known = [".geojson", ".json", ".zip", ".kml", ".shp", ".csv", ".txt"]
        for candidate in known:
            if lower.endswith(candidate):
                ext = candidate
                break

    target = os.path.join(output_folder, "{}{}".format(prefix, ext))
    if not os.path.exists(target):
        return target

    if if_exists_mode == "Fail":
        raise RuntimeError("Output file already exists and If File Exists=Fail: {}".format(target))

    if if_exists_mode == "Overwrite":
        try:
            os.remove(target)
        except Exception as ex:
            raise RuntimeError("Cannot overwrite existing file '{}': {}".format(target, ex))
        return target

    if if_exists_mode == "SuffixTimestamp":
        return os.path.join(output_folder, "{}_{}{}".format(prefix, _timestamp_compact(), ext))

    raise RuntimeError("Unsupported If File Exists mode: {}".format(if_exists_mode))


def _ensure_output_gdb(gdb_path):
    if not gdb_path:
        raise ValueError("Output File Geodatabase is required.")
    if not gdb_path.lower().endswith(".gdb"):
        raise ValueError("Output File Geodatabase must be a .gdb path: {}".format(gdb_path))

    if arcpy.Exists(gdb_path):
        return gdb_path

    parent = os.path.dirname(gdb_path)
    gdb_name = os.path.basename(gdb_path)
    if not os.path.isdir(parent):
        raise ValueError("Parent folder does not exist for GDB creation: {}".format(parent))

    arcpy.management.CreateFileGDB(parent, os.path.splitext(gdb_name)[0])
    return gdb_path


def _resolve_output_fc(output_gdb, requested_name, mode):
    timestamp = _timestamp_compact()
    validated_name = arcpy.ValidateTableName(requested_name, output_gdb)
    out_fc = os.path.join(output_gdb, validated_name)

    if not arcpy.Exists(out_fc):
        return out_fc, validated_name

    if mode == "Fail":
        raise RuntimeError("Output already exists and If Output Exists=Fail: {}".format(out_fc))

    if mode == "Overwrite":
        arcpy.management.Delete(out_fc)
        return out_fc, validated_name

    if mode == "SuffixTimestamp":
        suffix_name = "{}_{}".format(validated_name, timestamp)
        suffix_name = arcpy.ValidateTableName(suffix_name, output_gdb)
        candidate = os.path.join(output_gdb, suffix_name)
        if not arcpy.Exists(candidate):
            return candidate, suffix_name
        counter = 1
        while True:
            alt_name = arcpy.ValidateTableName("{}_{}_{}".format(validated_name, timestamp, counter), output_gdb)
            alt_fc = os.path.join(output_gdb, alt_name)
            if not arcpy.Exists(alt_fc):
                return alt_fc, alt_name
            counter += 1

    raise RuntimeError("Unsupported If Output Exists mode: {}".format(mode))


def _pick_resource(resources, preferred):
    if not resources:
        raise RuntimeError("No CKAN resources available in dataset.")

    geojson_candidates = [r for r in resources if _resource_is_geojson(r)]

    if preferred == "GEOJSON_ONLY":
        if not geojson_candidates:
            raise RuntimeError("Preferred Resource=GEOJSON_ONLY but no GeoJSON resource was found.")
        return geojson_candidates[0], True

    if geojson_candidates:
        return geojson_candidates[0], True

    # AUTO_PICK fallback
    downloadable = [r for r in resources if str(r.get("url", "")).strip()]
    if downloadable:
        return downloadable[0], False

    raise RuntimeError("No downloadable resource URL found in CKAN resources.")


def _count_rows(dataset):
    return int(arcpy.management.GetCount(dataset)[0])


def _list_fields_map(fc):
    return {f.name.lower(): f for f in arcpy.ListFields(fc)}


def _ensure_field(fc, name, field_type, length=None):
    fmap = _list_fields_map(fc)
    if name.lower() in fmap:
        return
    if field_type.upper() == "TEXT":
        out_len = int(length) if length else 255
        arcpy.management.AddField(fc, name, "TEXT", "", "", out_len)
    else:
        arcpy.management.AddField(fc, name, field_type)


def _clip_text(value, max_len):
    if value is None:
        return None
    text_value = str(value)
    if max_len is None:
        return text_value
    max_len = int(max_len)
    if len(text_value) <= max_len:
        return text_value
    if max_len <= 0:
        return ""
    return text_value[:max_len]


def _same_catalog_path(path_a, path_b):
    try:
        a = os.path.normcase(os.path.abspath(arcpy.Describe(path_a).catalogPath))
    except Exception:
        try:
            a = os.path.normcase(os.path.abspath(path_a))
        except Exception:
            a = os.path.normcase(str(path_a or ""))
    try:
        b = os.path.normcase(os.path.abspath(arcpy.Describe(path_b).catalogPath))
    except Exception:
        try:
            b = os.path.normcase(os.path.abspath(path_b))
        except Exception:
            b = os.path.normcase(str(path_b or ""))
    return a == b


def _unique_value_item_text(item):
    values = getattr(item, "values", None)
    if values:
        try:
            first_value = values[0][0]
            if first_value is not None:
                return str(first_value)
        except Exception:
            pass
        try:
            first_value = values[0]
            if first_value is not None:
                return str(first_value)
        except Exception:
            pass
    label = getattr(item, "label", None)
    if label is not None:
        return str(label)
    return ""


def _apply_gtfs_vehicle_symbology_to_layer(layer_obj):
    vehicle_type_colors = {
        "Bus": [128, 255, 81, 100],
        "Train": [1, 213, 255, 100],
        "Tram": [255, 60, 70, 100],
    }

    sym = layer_obj.symbology
    sym.updateRenderer("UniqueValueRenderer")
    sym.renderer.fields = ["VehicleType"]

    try:
        sym.renderer.addValues({"VehicleType": ["Bus", "Train", "Tram"]})
    except Exception:
        pass

    try:
        if hasattr(sym.renderer, "defaultSymbol") and sym.renderer.defaultSymbol:
            sym.renderer.defaultSymbol.size = 8
    except Exception:
        pass

    for group in getattr(sym.renderer, "groups", []):
        for item in getattr(group, "items", []):
            value_text = _unique_value_item_text(item).strip()
            if value_text in vehicle_type_colors:
                item.label = value_text
                item.symbol.color = {"RGB": vehicle_type_colors[value_text]}
                item.symbol.size = 8
            else:
                try:
                    item.symbol.size = 8
                except Exception:
                    pass

    layer_obj.symbology = sym


def _create_cim_rgb_color(red, green, blue, alpha=100, cim_version="V3"):
    try:
        color = arcpy.cim.CreateCIMObjectFromClassName("CIMRGBColor", cim_version)
        color.values = [red, green, blue, alpha]
        return color
    except Exception:
        return None


def _create_cim_white_halo_symbol(cim_version="V3"):
    try:
        polygon_symbol = arcpy.cim.CreateCIMObjectFromClassName("CIMPolygonSymbol", cim_version)
        solid_fill = arcpy.cim.CreateCIMObjectFromClassName("CIMSolidFill", cim_version)
        white = _create_cim_rgb_color(255, 255, 255, 100, cim_version)
        if white is not None:
            solid_fill.color = white
        try:
            solid_fill.enable = True
        except Exception:
            pass
        polygon_symbol.symbolLayers = [solid_fill]
        return polygon_symbol
    except Exception:
        return None


def _ensure_text_symbol_white_halo(text_symbol, cim_version="V3"):
    text_symbol.height = 8
    text_symbol.haloSize = 0.5

    halo_symbol = getattr(text_symbol, "haloSymbol", None)
    halo_layers = getattr(halo_symbol, "symbolLayers", None) if halo_symbol is not None else None

    if halo_symbol is None or not halo_layers:
        new_halo_symbol = _create_cim_white_halo_symbol(cim_version)
        if new_halo_symbol is not None:
            text_symbol.haloSymbol = new_halo_symbol
            halo_symbol = new_halo_symbol

    for symbol_layer in getattr(halo_symbol, "symbolLayers", []) or []:
        try:
            symbol_layer.enable = True
        except Exception:
            pass
        try:
            symbol_layer.color.values = [255, 255, 255, 100]
        except Exception:
            try:
                white = _create_cim_rgb_color(255, 255, 255, 100, cim_version)
                if white is not None:
                    symbol_layer.color = white
            except Exception:
                pass


def _apply_gtfs_route_id_labels_to_layer(layer_obj):
    layer_obj.showLabels = True

    label_classes = layer_obj.listLabelClasses()
    if label_classes:
        label_class = label_classes[0]
        label_class.visible = True
        try:
            label_class.expressionEngine = "Arcade"
        except Exception:
            pass
        label_class.expression = "$feature.RouteID"

    cim_version = "V3"
    try:
        cim_layer = layer_obj.getDefinition(cim_version)
    except Exception:
        cim_version = "V2"
        try:
            cim_layer = layer_obj.getDefinition(cim_version)
        except Exception:
            return

    try:
        cim_layer.showLabels = True
    except Exception:
        pass

    for label_class in getattr(cim_layer, "labelClasses", []) or []:
        try:
            label_class.visible = True
        except Exception:
            pass
        try:
            label_class.expressionEngine = "Arcade"
        except Exception:
            pass
        try:
            label_class.expression = "$feature.RouteID"
        except Exception:
            pass

        try:
            _ensure_text_symbol_white_halo(label_class.textSymbol.symbol, cim_version)
        except Exception:
            pass

    try:
        layer_obj.setDefinition(cim_layer)
    except Exception:
        pass


def _apply_gtfs_vehicle_map_styling(vehicle_fc, log_message=None, log_warning=None):
    def _log(message):
        if log_message:
            log_message(message)

    def _warn(message):
        if log_warning:
            log_warning(message)

    if not vehicle_fc or not arcpy.Exists(vehicle_fc):
        _warn("Vehicle symbology was requested, but the vehicle position feature class was not found.")
        return False

    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except Exception as ex:
        _warn("Vehicle symbology was requested, but the current ArcGIS Pro project could not be opened: {}".format(ex))
        return False

    try:
        target_map = aprx.activeMap
    except Exception:
        target_map = None

    if target_map is None:
        maps = aprx.listMaps()
        target_map = maps[0] if maps else None

    if target_map is None:
        _warn("Vehicle symbology was requested, but no map is available in the current project.")
        return False

    target_layer = None
    try:
        for lyr in target_map.listLayers():
            try:
                if not lyr.supports("DATASOURCE"):
                    continue
                if _same_catalog_path(lyr.dataSource, vehicle_fc):
                    target_layer = lyr
                    break
            except Exception:
                continue
    except Exception:
        target_layer = None

    if target_layer is None:
        try:
            target_layer = target_map.addDataFromPath(vehicle_fc)
            _log("Added vehicle position layer to active map for styling.")
        except Exception as ex:
            _warn("Vehicle symbology was requested, but the output layer could not be added to the active map: {}".format(ex))
            return False

    try:
        _apply_gtfs_vehicle_symbology_to_layer(target_layer)
    except Exception as ex:
        _warn("VehicleType unique value symbology could not be applied: {}".format(ex))
        return False

    try:
        _apply_gtfs_route_id_labels_to_layer(target_layer)
    except Exception as ex:
        _warn("RouteID labels could not be applied: {}".format(ex))

    _log("Applied VehicleType unique value symbology: Bus #80FF51, Train #01D5FF, Tram #FF3C46; marker size 8; RouteID labels size 8 with explicit 0.5 white halo symbol enabled where supported.")
    return True


def _safe_field_name(raw_name, workspace, used_names):
    candidate = re.sub(r"[^0-9A-Za-z_]", "_", (raw_name or "").strip())
    if not candidate:
        candidate = "field"
    if re.match(r"^[0-9]", candidate):
        candidate = "f_{}".format(candidate)
    candidate = candidate[:64]
    candidate = arcpy.ValidateFieldName(candidate, workspace)
    candidate = candidate[:64]
    if re.match(r"^[0-9]", candidate):
        candidate = "f_{}".format(candidate)
    base = candidate[:64]
    idx = 1
    while candidate.lower() in used_names:
        suffix = "_{}".format(idx)
        candidate = "{}{}".format(base[: 64 - len(suffix)], suffix)
        idx += 1
    used_names.add(candidate.lower())
    return candidate


def _infer_property_type(existing_type, value):
    if value is None:
        return existing_type

    value_type = None
    if isinstance(value, bool):
        value_type = "LONG"
    elif isinstance(value, int):
        value_type = "LONG"
    elif isinstance(value, float):
        value_type = "DOUBLE"
    else:
        value_type = "TEXT"

    if existing_type is None:
        return value_type

    if existing_type == value_type:
        return existing_type

    if existing_type in ("LONG", "DOUBLE") and value_type in ("LONG", "DOUBLE"):
        return "DOUBLE"

    return "TEXT"


def _coerce_property_value(value, target_type):
    if value is None:
        return None

    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=True, sort_keys=True)

    if target_type == "LONG":
        try:
            if isinstance(value, bool):
                return 1 if value else 0
            return int(value)
        except Exception:
            return None

    if target_type == "DOUBLE":
        try:
            return float(value)
        except Exception:
            return None

    # TEXT
    return str(value)


def _geojson_spatial_reference(geojson_obj):
    crs = geojson_obj.get("crs") if isinstance(geojson_obj, dict) else None
    if isinstance(crs, dict):
        props = crs.get("properties") if isinstance(crs.get("properties"), dict) else {}
        name_val = str(props.get("name", ""))
        match = re.search(r"EPSG[:/](\d+)", name_val, flags=re.IGNORECASE)
        if match:
            try:
                wkid = int(match.group(1))
                return arcpy.SpatialReference(wkid)
            except Exception:
                pass
    return arcpy.SpatialReference(4326)


def _build_polyline(geometry_obj, spatial_ref):
    if not isinstance(geometry_obj, dict):
        return None

    gtype = geometry_obj.get("type")
    coords = geometry_obj.get("coordinates")

    if gtype == "LineString":
        if not isinstance(coords, list) or len(coords) < 2:
            return None
        part = arcpy.Array()
        for xy in coords:
            if not isinstance(xy, (list, tuple)) or len(xy) < 2:
                continue
            try:
                part.add(arcpy.Point(float(xy[0]), float(xy[1])))
            except Exception:
                continue
        if len(part) < 2:
            return None
        return arcpy.Polyline(part, spatial_ref)

    if gtype == "MultiLineString":
        if not isinstance(coords, list) or len(coords) == 0:
            return None
        outer = arcpy.Array()
        for segment in coords:
            if not isinstance(segment, list) or len(segment) < 2:
                continue
            part = arcpy.Array()
            for xy in segment:
                if not isinstance(xy, (list, tuple)) or len(xy) < 2:
                    continue
                try:
                    part.add(arcpy.Point(float(xy[0]), float(xy[1])))
                except Exception:
                    continue
            if len(part) >= 2:
                outer.add(part)
        if len(outer) == 0:
            return None
        return arcpy.Polyline(outer, spatial_ref)

    return None


def _manual_geojson_to_fc(geojson_path, scratch_gdb, allow_non_polyline, log_message, log_warning):
    with open(geojson_path, "r", encoding="utf-8") as fobj:
        data = json.load(fobj)

    if not isinstance(data, dict):
        raise RuntimeError("GeoJSON root must be an object.")

    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
    elif data.get("type") == "Feature":
        features = [data]
    else:
        # Attempt to treat as geometry object
        features = [{"type": "Feature", "geometry": data, "properties": {}}]

    if not isinstance(features, list) or len(features) == 0:
        raise RuntimeError("GeoJSON contains no features.")

    source_sr = _geojson_spatial_reference(data)

    # Infer property schema from line features only to keep output deterministic.
    prop_info = {}
    text_lengths = {}
    line_features = []
    skipped_non_line = 0

    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if not isinstance(geom, dict):
            continue
        gtype = geom.get("type")

        if gtype not in ("LineString", "MultiLineString"):
            skipped_non_line += 1
            if not allow_non_polyline:
                raise RuntimeError(
                    "Found non-polyline geometry '{}' and Allow Non-Polyline=False.".format(gtype)
                )
            continue

        line_features.append(feat)
        props = feat.get("properties")
        if not isinstance(props, dict):
            continue

        for key, value in sorted(props.items(), key=lambda kv: str(kv[0])):
            if key not in prop_info:
                prop_info[key] = None
                text_lengths[key] = 0
            inferred = _infer_property_type(prop_info[key], value)
            prop_info[key] = inferred
            if value is not None:
                if isinstance(value, (dict, list, tuple)):
                    val_text = json.dumps(value, ensure_ascii=True, sort_keys=True)
                else:
                    val_text = str(value)
                if len(val_text) > text_lengths[key]:
                    text_lengths[key] = len(val_text)

    if skipped_non_line > 0:
        log_warning("Manual import skipped {} non-polyline features.".format(skipped_non_line))

    if len(line_features) == 0:
        raise RuntimeError("No line features were available for manual GeoJSON import.")

    # Create schema
    out_name = arcpy.CreateUniqueName("bd_manual_import", scratch_gdb)
    out_fc = arcpy.management.CreateFeatureclass(
        scratch_gdb,
        os.path.basename(out_name),
        "POLYLINE",
        spatial_reference=source_sr,
    )[0]

    used_names = set([f.name.lower() for f in arcpy.ListFields(out_fc)])
    field_schema = []

    for key in sorted(prop_info.keys(), key=lambda x: str(x).lower()):
        inferred_type = prop_info[key] or "TEXT"
        if inferred_type not in ("LONG", "DOUBLE", "TEXT"):
            inferred_type = "TEXT"

        safe_name = _safe_field_name(str(key), scratch_gdb, used_names)
        out_type = inferred_type
        out_len = None
        if out_type == "TEXT":
            out_len = min(max(text_lengths.get(key, 0), 32), 255)
            arcpy.management.AddField(out_fc, safe_name, "TEXT", "", "", out_len)
        elif out_type == "LONG":
            arcpy.management.AddField(out_fc, safe_name, "LONG")
        else:
            arcpy.management.AddField(out_fc, safe_name, "DOUBLE")

        field_schema.append(
            {
                "src": key,
                "dst": safe_name,
                "type": out_type,
                "length": out_len,
            }
        )

    cursor_fields = ["SHAPE@"] + [f["dst"] for f in field_schema]

    total = len(line_features)
    inserted = 0
    invalid_geom = 0

    arcpy.SetProgressor("step", "Manual GeoJSON import...", 0, total, 1)

    with arcpy.da.InsertCursor(out_fc, cursor_fields) as icur:
        for idx, feat in enumerate(line_features, start=1):
            geom = _build_polyline(feat.get("geometry"), source_sr)
            if _geometry_is_empty(geom):
                invalid_geom += 1
                continue

            row_vals = [geom]
            props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
            for fd in field_schema:
                raw_val = props.get(fd["src"])
                coerced = _coerce_property_value(raw_val, fd["type"])
                if fd["type"] == "TEXT" and coerced is not None:
                    coerced = str(coerced)[: fd["length"]]
                row_vals.append(coerced)

            icur.insertRow(row_vals)
            inserted += 1

            if idx % 1000 == 0:
                log_message("Manual import progress: inserted {} / {} features.".format(inserted, total))
            arcpy.SetProgressorPosition()

    if invalid_geom > 0:
        log_warning("Manual import skipped {} invalid/empty line geometries.".format(invalid_geom))

    if inserted == 0:
        raise RuntimeError("Manual GeoJSON import inserted zero features.")

    return out_fc, inserted, skipped_non_line, invalid_geom


def _detect_field_name(all_fields, candidates):
    if not all_fields:
        return None

    norm_map = {}
    for fld in all_fields:
        norm_map[_normalize_name(fld.name)] = fld.name

    for cand in candidates:
        n = _normalize_name(cand)
        if n in norm_map:
            return norm_map[n]

    # Relaxed contains check
    for fld in all_fields:
        fn = _normalize_name(fld.name)
        for cand in candidates:
            cn = _normalize_name(cand)
            if cn and cn in fn:
                return fld.name

    return None


def _parse_dir_code(raw_value):
    if raw_value is None:
        return 0
    s = str(raw_value).strip().lower()
    if not s:
        return 0

    both = {
        "0",
        "b",
        "both",
        "two-way",
        "two way",
        "bidirectional",
        "bi",
        "ft_tf",
        "tf_ft",
        "yes",
        "y",
    }
    from_to = {"1", "ft", "fromto", "from-to", "with", "forward", "fwd", "oneway_ft", "ab", "a_b"}
    to_from = {"2", "tf", "tofrom", "to-from", "reverse", "backward", "oneway_tf", "ba", "b_a"}

    if s in both:
        return 0
    if s in from_to:
        return 1
    if s in to_from:
        return 2

    if "from" in s and "to" in s and "-" in s:
        if s.startswith("to"):
            return 2
        return 1

    if "->" in s:
        return 1
    if "<-" in s:
        return 2

    return 0


def _normalize_one_way_value(raw_value):
    clean = ""
    if raw_value is not None:
        clean = str(raw_value).strip().upper()

    if clean in ("", "N", "2W"):
        return 0, clean, False, None
    if clean == "22":
        return 0, clean, True, None
    if clean == "FT":
        return 1, clean, False, None
    if clean == "TF":
        return 2, clean, False, None

    return 0, clean, False, clean


def _parse_path_type_selection(raw_value):
    if raw_value is None:
        return []
    txt = str(raw_value).strip()
    if not txt:
        return []
    values = [part.strip() for part in txt.split(";") if part and part.strip()]
    if len(values) == 1 and values[0].upper() == "ALL":
        return []
    return values


def _sql_in_clause_text(dataset_path, field_name, values):
    if not values:
        return "1=0"
    field_expr = arcpy.AddFieldDelimiters(dataset_path, field_name)
    quoted = []
    for value in values:
        quoted.append("'{}'".format(str(value).replace("'", "''")))
    return "{} IN ({})".format(field_expr, ", ".join(quoted))


def _class_rule(class_text):
    if class_text is None:
        return None

    s = str(class_text).strip().lower()
    if not s:
        return None

    rules = [
        {
            "patterns": ["shared path", "off road", "off-road", "trail", "greenway", "separated"],
            "bike": 20.0,
            "ebike": 28.0,
            "bike_access": 1,
            "ebike_access": 1,
            "normalized": "shared_path",
        },
        {
            "patterns": ["bike lane", "painted", "on road", "on-road", "cycle lane"],
            "bike": 16.0,
            "ebike": 24.0,
            "bike_access": 1,
            "ebike_access": 1,
            "normalized": "bike_lane",
        },
        {
            "patterns": ["local", "street", "collector", "roadway"],
            "bike": 15.0,
            "ebike": 22.0,
            "bike_access": 1,
            "ebike_access": 1,
            "normalized": "local_road",
        },
        {
            "patterns": ["steps", "stair", "pedestrian only", "footpath only", "no bike"],
            "bike": 4.0,
            "ebike": 4.0,
            "bike_access": 0,
            "ebike_access": 0,
            "normalized": "restricted_or_steps",
        },
        {
            "patterns": ["steep"],
            "bike": 8.0,
            "ebike": 10.0,
            "bike_access": 1,
            "ebike_access": 1,
            "normalized": "steep",
        },
    ]

    for rule in rules:
        if any(pat in s for pat in rule["patterns"]):
            return rule

    return {
        "bike": None,
        "ebike": None,
        "bike_access": 1,
        "ebike_access": 1,
        "normalized": s[:100],
    }


def _blocked_access_from_text(text_blob):
    if not text_blob:
        return 1, 1

    s = text_blob.lower()

    bike_block = [
        "no bike",
        "no bicycle",
        "bicycle prohibited",
        "cycling prohibited",
        "closed to bikes",
        "pedestrian only",
        "footpath only",
        "stairs",
        "steps",
    ]
    ebike_block = [
        "no ebike",
        "no e-bike",
        "no electric bike",
        "motor prohibited",
        "motorized prohibited",
        "pedestrian only",
        "footpath only",
        "stairs",
        "steps",
    ]

    bike_access = 1
    ebike_access = 1

    if any(k in s for k in bike_block):
        bike_access = 0
    if any(k in s for k in ebike_block):
        ebike_access = 0

    return bike_access, ebike_access


def _strict_trim_text_fields(fc):
    text_fields = [f.name for f in arcpy.ListFields(fc) if f.type == "String"]
    if not text_fields:
        return 0, 0

    changed_count = 0
    nullified_count = 0
    with arcpy.da.UpdateCursor(fc, text_fields) as ucur:
        for row in ucur:
            changed = False
            for i, val in enumerate(row):
                if val is None:
                    continue
                s = str(val)
                trimmed = s.strip()
                if trimmed != s:
                    changed = True
                    changed_count += 1
                if trimmed == "":
                    if row[i] is not None:
                        row[i] = None
                        changed = True
                        nullified_count += 1
                else:
                    row[i] = trimmed
            if changed:
                ucur.updateRow(row)

    return changed_count, nullified_count


def _multipart_count(fc):
    count = 0
    with arcpy.da.SearchCursor(fc, ["SHAPE@"]) as cur:
        for (geom,) in cur:
            try:
                if geom and geom.partCount > 1:
                    count += 1
            except Exception:
                continue
    return count


def _endpoint_fragmentation_ratio(fc, scratch_gdb):
    end_fc = arcpy.CreateUniqueName("bd_endpoints", scratch_gdb)
    ident_tbl = arcpy.CreateUniqueName("bd_end_ident", scratch_gdb)

    arcpy.management.FeatureVerticesToPoints(fc, end_fc, "BOTH_ENDS")
    endpoint_count = _count_rows(end_fc)
    if endpoint_count == 0:
        return 0.0, 0, 0

    arcpy.management.FindIdentical(end_fc, ident_tbl, ["Shape"], "", "", "ALL")

    seq_counts = {}
    with arcpy.da.SearchCursor(ident_tbl, ["FEAT_SEQ"]) as cur:
        for (seq,) in cur:
            seq_counts[seq] = seq_counts.get(seq, 0) + 1

    unique_nodes = sum(1 for c in seq_counts.values() if c == 1)
    ratio = float(unique_nodes) / float(endpoint_count)
    return ratio, endpoint_count, unique_nodes


def _write_qa_csv_safe(out_folder, base_name, qa_rows, log_warning):
    if not os.path.isdir(out_folder):
        raise RuntimeError("Output folder does not exist for QA CSV: {}".format(out_folder))

    final_path = os.path.join(out_folder, "{}.csv".format(base_name))

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="{}_".format(base_name), suffix=".tmp", dir=out_folder)
    os.close(tmp_fd)

    try:
        with open(tmp_path, "w", newline="", encoding="utf-8") as fobj:
            writer = csv.writer(fobj)
            writer.writerow(["Metric", "Value"])
            for key, value in qa_rows:
                writer.writerow([key, value])

        try:
            os.replace(tmp_path, final_path)
            return final_path
        except Exception:
            alt_path = os.path.join(out_folder, "{}_{}.csv".format(base_name, _timestamp_compact()))
            os.replace(tmp_path, alt_path)
            log_warning("QA CSV target was locked/unavailable. Wrote alternate file: {}".format(alt_path))
            return alt_path
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _fetch_bytes_with_retry(url, retries=3, timeout=30):
    last_error = None
    headers = {
        "User-Agent": "ArcGISPro-AdelaideMetro-GTFSRT/1.0",
        "Accept": "application/x-google-protobuf,application/octet-stream,*/*",
    }
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = getattr(resp, "status", None)
                if code is not None and int(code) >= 400:
                    raise RuntimeError("HTTP {} for URL {}".format(code, url))
                return resp.read()
        except Exception as ex:
            last_error = ex
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
            else:
                raise RuntimeError("Failed to fetch URL after {} tries: {}".format(retries, last_error))


def _pb_read_varint(buf, idx):
    value = 0
    shift = 0
    while True:
        if idx >= len(buf):
            raise ValueError("Unexpected EOF while reading varint.")
        b = buf[idx]
        idx += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, idx
        shift += 7
        if shift > 70:
            raise ValueError("Invalid varint (too many bytes).")


def _pb_decode_message(buf):
    idx = 0
    out = {}
    while idx < len(buf):
        key, idx = _pb_read_varint(buf, idx)
        field_no = key >> 3
        wire_type = key & 0x07

        if wire_type == 0:  # varint
            value, idx = _pb_read_varint(buf, idx)
            item = (wire_type, value)
        elif wire_type == 1:  # 64-bit
            if idx + 8 > len(buf):
                raise ValueError("Unexpected EOF while reading fixed64.")
            item = (wire_type, buf[idx : idx + 8])
            idx += 8
        elif wire_type == 2:  # length-delimited
            ln, idx = _pb_read_varint(buf, idx)
            if idx + ln > len(buf):
                raise ValueError("Unexpected EOF while reading length-delimited field.")
            item = (wire_type, buf[idx : idx + ln])
            idx += ln
        elif wire_type == 5:  # 32-bit
            if idx + 4 > len(buf):
                raise ValueError("Unexpected EOF while reading fixed32.")
            item = (wire_type, buf[idx : idx + 4])
            idx += 4
        else:
            raise ValueError("Unsupported protobuf wire type: {}".format(wire_type))

        out.setdefault(field_no, []).append(item)
    return out


def _pb_first_varint(msg, field_no):
    for wire, value in msg.get(field_no, []):
        if wire == 0:
            return value
    return None


def _pb_first_string(msg, field_no):
    for wire, value in msg.get(field_no, []):
        if wire == 2:
            try:
                return value.decode("utf-8")
            except Exception:
                return value.decode("utf-8", "replace")
    return None


def _pb_submessages(msg, field_no):
    out = []
    for wire, value in msg.get(field_no, []):
        if wire == 2:
            out.append(_pb_decode_message(value))
    return out


def _pb_first_float32(msg, field_no):
    for wire, value in msg.get(field_no, []):
        if wire == 5 and len(value) == 4:
            return struct.unpack("<f", value)[0]
    return None


def _pb_first_float64(msg, field_no):
    for wire, value in msg.get(field_no, []):
        if wire == 1 and len(value) == 8:
            return struct.unpack("<d", value)[0]
    return None


def _pb_int32_from_varint(value):
    if value is None:
        return None
    if value >= (1 << 31):
        value -= (1 << 32)
    return int(value)


def _gtfs_epoch_to_datetime(epoch_value):
    if epoch_value is None:
        return None
    try:
        return datetime.datetime.utcfromtimestamp(float(epoch_value))
    except Exception:
        return None


def _gtfs_vehicle_type(route_id):
    if not route_id:
        return "Unknown"
    rid = str(route_id).strip().upper()
    if rid in ("GLNELG", "BTANIC"):
        return "Tram"
    if rid.isalpha():
        return "Train"
    return "Bus"


def _gtfs_parse_root_and_timestamp(feed_bytes):
    root = _pb_decode_message(feed_bytes)
    header_list = _pb_submessages(root, 1)
    header = header_list[0] if header_list else {}
    ts_epoch = _pb_first_varint(header, 3)
    return root, _gtfs_epoch_to_datetime(ts_epoch)


def _gtfs_parse_translation(translation_msg):
    parts = []
    for translated in _pb_submessages(translation_msg, 1):
        text = _pb_first_string(translated, 1)
        lang = _pb_first_string(translated, 2)
        if text:
            if lang:
                parts.append("{}:{}".format(lang, text))
            else:
                parts.append(text)
    return " | ".join(parts)


def _gtfs_parse_vehicle_positions(feed_bytes, enforce_adelaide_bounds=True):
    root, feed_ts = _gtfs_parse_root_and_timestamp(feed_bytes)
    rows = []
    for entity in _pb_submessages(root, 2):
        vehicle_msgs = _pb_submessages(entity, 4)
        if not vehicle_msgs:
            continue

        vehicle = vehicle_msgs[0]
        trip_msgs = _pb_submessages(vehicle, 1)
        position_msgs = _pb_submessages(vehicle, 2)
        descriptor_msgs = _pb_submessages(vehicle, 8)

        trip = trip_msgs[0] if trip_msgs else {}
        position = position_msgs[0] if position_msgs else {}
        descriptor = descriptor_msgs[0] if descriptor_msgs else {}

        lat = _pb_first_float32(position, 1)
        lon = _pb_first_float32(position, 2)

        if lat is None or lon is None:
            continue
        if enforce_adelaide_bounds:
            if not (-36.5 <= lat <= -33.5 and 137.5 <= lon <= 140.5):
                continue

        record = {
            "FeedTimestamp": feed_ts,
            "EntityID": _pb_first_string(entity, 1),
            "VehicleID": _pb_first_string(descriptor, 1),
            "VehicleLabel": _pb_first_string(descriptor, 2),
            "LicensePlate": _pb_first_string(descriptor, 3),
            "TripID": _pb_first_string(trip, 1),
            "RouteID": _pb_first_string(trip, 5),
            "DirectionID": _pb_first_varint(trip, 6),
            "StartTime": _pb_first_string(trip, 2),
            "StartDate": _pb_first_string(trip, 3),
            "Latitude": lat,
            "Longitude": lon,
            "Bearing": _pb_first_float32(position, 3),
            "Speed": _pb_first_float32(position, 5),
            "Odometer": _pb_first_float64(position, 4),
            "CurrentStopID": _pb_first_string(vehicle, 7),
            "CurrentStopSeq": _pb_first_varint(vehicle, 3),
            "CurrentStatus": _pb_first_varint(vehicle, 4),
            "PositionTimestamp": _gtfs_epoch_to_datetime(_pb_first_varint(vehicle, 5)),
        }
        record["VehicleType"] = _gtfs_vehicle_type(record.get("RouteID"))
        rows.append(record)

    return rows, feed_ts


def _gtfs_parse_trip_updates(feed_bytes):
    root, feed_ts = _gtfs_parse_root_and_timestamp(feed_bytes)
    rows = []
    for entity in _pb_submessages(root, 2):
        update_msgs = _pb_submessages(entity, 3)
        if not update_msgs:
            continue

        upd = update_msgs[0]
        trip_msgs = _pb_submessages(upd, 1)
        vehicle_msgs = _pb_submessages(upd, 3)
        stop_updates = _pb_submessages(upd, 2)

        trip = trip_msgs[0] if trip_msgs else {}
        vehicle = vehicle_msgs[0] if vehicle_msgs else {}

        first_stop = stop_updates[0] if stop_updates else {}
        arrival_msgs = _pb_submessages(first_stop, 2)
        departure_msgs = _pb_submessages(first_stop, 3)
        arrival = arrival_msgs[0] if arrival_msgs else {}
        departure = departure_msgs[0] if departure_msgs else {}

        record = {
            "FeedTimestamp": feed_ts,
            "EntityID": _pb_first_string(entity, 1),
            "TripID": _pb_first_string(trip, 1),
            "RouteID": _pb_first_string(trip, 5),
            "DirectionID": _pb_first_varint(trip, 6),
            "StartTime": _pb_first_string(trip, 2),
            "StartDate": _pb_first_string(trip, 3),
            "VehicleID": _pb_first_string(vehicle, 1),
            "VehicleLabel": _pb_first_string(vehicle, 2),
            "TripTimestamp": _gtfs_epoch_to_datetime(_pb_first_varint(upd, 4)),
            "DelaySeconds": _pb_int32_from_varint(_pb_first_varint(upd, 5)),
            "StopTimeUpdateCount": len(stop_updates),
            "FirstStopID": _pb_first_string(first_stop, 4),
            "FirstArrivalTime": _gtfs_epoch_to_datetime(_pb_first_varint(arrival, 2)),
            "FirstDepartureTime": _gtfs_epoch_to_datetime(_pb_first_varint(departure, 2)),
        }
        rows.append(record)

    return rows, feed_ts


def _gtfs_parse_service_alerts(feed_bytes):
    root, feed_ts = _gtfs_parse_root_and_timestamp(feed_bytes)
    rows = []
    for entity in _pb_submessages(root, 2):
        alert_msgs = _pb_submessages(entity, 5)
        if not alert_msgs:
            continue

        alert = alert_msgs[0]
        header_msgs = _pb_submessages(alert, 10)
        desc_msgs = _pb_submessages(alert, 11)
        url_msgs = _pb_submessages(alert, 8)
        period_msgs = _pb_submessages(alert, 1)
        informed = _pb_submessages(alert, 5)

        header_text = _gtfs_parse_translation(header_msgs[0]) if header_msgs else ""
        desc_text = _gtfs_parse_translation(desc_msgs[0]) if desc_msgs else ""
        url_text = _gtfs_parse_translation(url_msgs[0]) if url_msgs else ""

        first_period = period_msgs[0] if period_msgs else {}
        start_dt = _gtfs_epoch_to_datetime(_pb_first_varint(first_period, 1))
        end_dt = _gtfs_epoch_to_datetime(_pb_first_varint(first_period, 2))

        route_ids = set()
        stop_ids = set()
        trip_ids = set()
        for inf in informed:
            route_id = _pb_first_string(inf, 2)
            stop_id = _pb_first_string(inf, 5)
            trip_msgs = _pb_submessages(inf, 4)
            trip = trip_msgs[0] if trip_msgs else {}
            trip_id = _pb_first_string(trip, 1)
            if route_id:
                route_ids.add(route_id)
            if stop_id:
                stop_ids.add(stop_id)
            if trip_id:
                trip_ids.add(trip_id)

        rows.append(
            {
                "FeedTimestamp": feed_ts,
                "EntityID": _pb_first_string(entity, 1),
                "Cause": _pb_first_varint(alert, 6),
                "Effect": _pb_first_varint(alert, 7),
                "Severity": _pb_first_varint(alert, 14),
                "HeaderText": header_text,
                "DescriptionText": desc_text,
                "UrlText": url_text,
                "ActiveStart": start_dt,
                "ActiveEnd": end_dt,
                "InformedEntityCount": len(informed),
                "RouteIDs": ", ".join(sorted(route_ids)),
                "StopIDs": ", ".join(sorted(stop_ids)),
                "TripIDs": ", ".join(sorted(trip_ids)),
            }
        )

    return rows, feed_ts


class Toolbox(object):
    def __init__(self):
        self.label = "Urban Operating System Transport Tools"
        self.alias = "UrbanOperatingSystemTransportTools"
        self.tools = [BikeDirect_CKAN_Download_Enrich, CreateOSMNetworkDatasetGDB, OSMToNetworkDataset, AdelaideMetroRealtimePublicTransport]


class Fetch_Bike_Direct_Data_01(object):
    def __init__(self):
        self.label = "01-Fetch-Bike-Direct-Data"
        self.description = "Fetch Bike Direct dataset resource file from CKAN and download to a folder."
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="CKAN Base URL",
            name="ckan_base_url",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p0.value = "https://data.sa.gov.au"

        p1 = arcpy.Parameter(
            displayName="CKAN Dataset Slug",
            name="ckan_dataset_slug",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p1.value = "bike-direct-network"

        p2 = arcpy.Parameter(
            displayName="Preferred Resource",
            name="preferred_resource",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p2.filter.type = "ValueList"
        p2.filter.list = ["GEOJSON_ONLY", "AUTO_PICK"]
        p2.value = "AUTO_PICK"

        p3 = arcpy.Parameter(
            displayName="Output Folder",
            name="output_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input",
        )

        p4 = arcpy.Parameter(
            displayName="Output File Prefix",
            name="output_file_prefix",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        p4.value = "bikedirect"

        p5 = arcpy.Parameter(
            displayName="If File Exists",
            name="if_file_exists",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p5.filter.type = "ValueList"
        p5.filter.list = ["Fail", "Overwrite", "SuffixTimestamp"]
        p5.value = "SuffixTimestamp"

        p6 = arcpy.Parameter(
            displayName="Downloaded File",
            name="downloaded_file",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output",
        )

        return [p0, p1, p2, p3, p4, p5, p6]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        if not parameters[0].altered and not parameters[0].value:
            parameters[0].value = "https://data.sa.gov.au"
        if not parameters[1].altered and not parameters[1].value:
            parameters[1].value = "bike-direct-network"
        if not parameters[4].altered and not parameters[4].value:
            parameters[4].value = "bikedirect"
        return

    def updateMessages(self, parameters):
        base_url = parameters[0].valueAsText
        out_folder = parameters[3].valueAsText
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            parameters[0].setErrorMessage("CKAN Base URL must start with http:// or https://")
        if out_folder and not os.path.isdir(out_folder):
            parameters[3].setErrorMessage("Output Folder must exist.")
        return

    def execute(self, parameters, messages):
        start_time = time.time()

        def log(msg):
            arcpy.AddMessage(msg)

        def warn(msg):
            arcpy.AddWarning(msg)

        def err(msg):
            arcpy.AddError(msg)

        try:
            arcpy.SetProgressor("step", "Fetch Bike Direct data", 0, 3, 1)

            ckan_base = (parameters[0].valueAsText or "").strip().rstrip("/")
            dataset_slug = (parameters[1].valueAsText or "").strip()
            preferred_resource = parameters[2].valueAsText or "AUTO_PICK"
            output_folder = parameters[3].valueAsText
            output_file_prefix = parameters[4].valueAsText or "bikedirect"
            if_file_exists = parameters[5].valueAsText or "SuffixTimestamp"

            # 1) CKAN package discovery
            arcpy.SetProgressorLabel("Step 1/3 - CKAN discovery")
            log("[1/3] CKAN discovery: requesting package metadata...")
            package_payload, package_url, failures = _discover_ckan_package(
                ckan_base, dataset_slug, log, warn
            )
            if failures:
                warn("CKAN fallback was required; failed endpoints: {}".format(" | ".join(failures)))

            if not isinstance(package_payload, dict):
                raise RuntimeError("CKAN response is not a JSON object.")
            if not package_payload.get("success", False):
                raise RuntimeError("CKAN package_show returned success=False.")

            result = package_payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("CKAN package_show missing result object.")
            resources = result.get("resources")
            if not isinstance(resources, list) or not resources:
                raise RuntimeError("CKAN package has no resources.")

            selected_resource, is_geojson = _pick_resource(resources, preferred_resource)
            resource_url = str(selected_resource.get("url", "")).strip()
            resource_format = str(selected_resource.get("format", "")).strip()
            if not resource_url:
                raise RuntimeError("Selected CKAN resource URL is empty.")
            if not is_geojson:
                warn(
                    "AUTO_PICK selected non-GeoJSON resource (format='{}').".format(resource_format)
                )
            log("CKAN metadata endpoint: {}".format(package_url))
            log("Selected resource URL: {}".format(resource_url))
            arcpy.SetProgressorPosition()

            # 2) Resolve output path
            arcpy.SetProgressorLabel("Step 2/3 - Resolve output path")
            log("[2/3] Resolving output file path...")
            out_file = _resolve_output_file_path(
                output_folder, output_file_prefix, resource_url, if_file_exists
            )
            log("Output download path: {}".format(out_file))
            arcpy.SetProgressorPosition()

            # 3) Download
            arcpy.SetProgressorLabel("Step 3/3 - Download resource")
            log("[3/3] Downloading resource...")
            _download_with_retry(resource_url, out_file, retries=3, timeout=180)
            file_size = os.path.getsize(out_file)
            if file_size <= 0:
                raise RuntimeError("Downloaded file is empty: {}".format(out_file))
            log("Downloaded {} bytes.".format(file_size))
            log("Elapsed seconds: {}".format(round(time.time() - start_time, 3)))

            arcpy.SetParameterAsText(6, out_file)

        except Exception as ex:
            err("Tool failed: {}".format(ex))
            err("CKAN base='{}', dataset='{}'".format(parameters[0].valueAsText, parameters[1].valueAsText))
            err(traceback.format_exc())
            raise
        finally:
            arcpy.ResetProgressor()


class BikeDirect_CKAN_Download_Enrich(object):
    def __init__(self):
        self.label = "1.1 Transport - Bike Direct Network Dataset Maker"
        self.description = (
            "Load local Bike Direct GeoJSON, run sanity checks, "
            "and create a network-ready enriched polyline feature class for BIKE and EBIKE."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="Input GeoJSON File",
            name="input_geojson_file",
            datatype="DEFile",
            parameterType="Required",
            direction="Input",
        )

        p1 = arcpy.Parameter(
            displayName="Output Folder (Temp/QA)",
            name="output_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input",
        )

        p2 = arcpy.Parameter(
            displayName="Output File Geodatabase",
            name="output_gdb",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input",
        )

        p3 = arcpy.Parameter(
            displayName="Output Feature Class Name",
            name="output_fc_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p3.value = "BikeDirect_Enriched"

        p4 = arcpy.Parameter(
            displayName="Target Spatial Reference",
            name="target_spatial_reference",
            datatype="GPSpatialReference",
            parameterType="Optional",
            direction="Input",
        )
        try:
            p4.value = arcpy.SpatialReference(7854)
        except Exception:
            pass

        p5 = arcpy.Parameter(
            displayName="Enrichment Mode",
            name="enrichment_mode",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p5.filter.type = "ValueList"
        p5.filter.list = ["Minimal", "Recommended", "Strict"]
        p5.value = "Recommended"

        p6 = arcpy.Parameter(
            displayName="Bike Speed Model",
            name="bike_speed_model",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p6.filter.type = "ValueList"
        p6.filter.list = ["Fixed", "ClassBased"]
        p6.value = "Fixed"

        p7 = arcpy.Parameter(
            displayName="E-bike Speed Model",
            name="ebike_speed_model",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p7.filter.type = "ValueList"
        p7.filter.list = ["Fixed", "ClassBased"]
        p7.value = "Fixed"

        p8 = arcpy.Parameter(
            displayName="Fixed Bike Speed (km/h)",
            name="fixed_bike_speed",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        p8.value = 15.0

        p9 = arcpy.Parameter(
            displayName="Fixed E-bike Speed (km/h)",
            name="fixed_ebike_speed",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        p9.value = 25.0

        p10 = arcpy.Parameter(
            displayName="Max Speed Cap Bike (km/h)",
            name="max_bike_cap",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        p10.value = 40.0

        p11 = arcpy.Parameter(
            displayName="Max Speed Cap E-bike (km/h)",
            name="max_ebike_cap",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        p11.value = 45.0

        p12 = arcpy.Parameter(
            displayName="Multipart Handling",
            name="multipart_handling",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p12.filter.type = "ValueList"
        p12.filter.list = ["Warn", "ExplodeToSinglepart"]
        p12.value = "Warn"

        p13 = arcpy.Parameter(
            displayName="Geometry Validation",
            name="geometry_validation",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p13.filter.type = "ValueList"
        p13.filter.list = ["CheckOnly", "RepairIfPossible"]
        p13.value = "CheckOnly"

        p14 = arcpy.Parameter(
            displayName="Included Path Types (multi-value; blank=ALL)",
            name="included_path_types",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        p14.multiValue = True

        p15 = arcpy.Parameter(
            displayName="Allow Non-Polyline",
            name="allow_non_polyline",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
        )
        p15.value = False

        p16 = arcpy.Parameter(
            displayName="Write QA Report",
            name="write_qa_report",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
        )
        p16.value = True

        p17 = arcpy.Parameter(
            displayName="If Output Exists",
            name="if_output_exists",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p17.filter.type = "ValueList"
        p17.filter.list = ["Fail", "Overwrite", "SuffixTimestamp"]
        p17.value = "SuffixTimestamp"

        p18 = arcpy.Parameter(
            displayName="Enriched Feature Class",
            name="enriched_feature_class",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output",
        )

        p19 = arcpy.Parameter(
            displayName="QA Table/CSV",
            name="qa_table_csv",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output",
        )

        return [
            p0,
            p1,
            p2,
            p3,
            p4,
            p5,
            p6,
            p7,
            p8,
            p9,
            p10,
            p11,
            p12,
            p13,
            p14,
            p15,
            p16,
            p17,
            p18,
            p19,
        ]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        # Keep user-entered values; only provide gentle defaults where empty.
        if not parameters[3].altered and not parameters[3].value:
            parameters[3].value = "BikeDirect_Enriched"
        return

    def updateMessages(self, parameters):
        local_geojson = parameters[0].valueAsText
        out_folder = parameters[1].valueAsText
        out_gdb = parameters[2].valueAsText

        if local_geojson and not os.path.isfile(local_geojson):
            parameters[0].setErrorMessage("Input GeoJSON File must exist.")
        if local_geojson and not local_geojson.lower().endswith((".geojson", ".json", ".zip")):
            parameters[0].setWarningMessage("Input file is not .geojson/.json/.zip; import may fail.")

        if out_folder and (not os.path.isdir(out_folder)):
            parameters[1].setErrorMessage("Output Folder must exist.")

        if out_gdb and (not out_gdb.lower().endswith(".gdb")):
            parameters[2].setErrorMessage("Output File Geodatabase must be a .gdb path.")

        if not parameters[4].value:
            parameters[4].setWarningMessage(
                "Target Spatial Reference is blank. Tool will fall back to geodesic lengths."
            )

        fixed_bike = _safe_float(parameters[8].value, None)
        fixed_ebike = _safe_float(parameters[9].value, None)
        cap_bike = _safe_float(parameters[10].value, None)
        cap_ebike = _safe_float(parameters[11].value, None)

        if fixed_bike is None or fixed_bike <= 0:
            parameters[8].setErrorMessage("Fixed Bike Speed must be > 0.")
        if fixed_ebike is None or fixed_ebike <= 0:
            parameters[9].setErrorMessage("Fixed E-bike Speed must be > 0.")
        if cap_bike is None or cap_bike <= 0:
            parameters[10].setErrorMessage("Max Speed Cap Bike must be > 0.")
        if cap_ebike is None or cap_ebike <= 0:
            parameters[11].setErrorMessage("Max Speed Cap E-bike must be > 0.")

        if fixed_bike is not None and cap_bike is not None and fixed_bike > cap_bike:
            parameters[8].setWarningMessage("Fixed Bike Speed exceeds cap and will be capped.")
        if fixed_ebike is not None and cap_ebike is not None and fixed_ebike > cap_ebike:
            parameters[9].setWarningMessage("Fixed E-bike Speed exceeds cap and will be capped.")

        return

    def execute(self, parameters, messages):
        start_time = time.time()
        warnings_list = []
        assumptions = []
        qa = {}

        def log(msg):
            arcpy.AddMessage(msg)

        def warn(msg):
            warnings_list.append(msg)
            arcpy.AddWarning(msg)

        def err(msg):
            arcpy.AddError(msg)

        try:
            arcpy.SetProgressor("step", "BikeDirect GeoJSON Enrich", 0, 7, 1)

            local_geojson_path = (parameters[0].valueAsText or "").strip()
            output_folder = parameters[1].valueAsText
            output_gdb = parameters[2].valueAsText
            output_fc_name = parameters[3].valueAsText or "BikeDirect_Enriched"
            target_sr = parameters[4].value
            enrichment_mode = parameters[5].valueAsText or "Recommended"
            bike_speed_model = parameters[6].valueAsText or "Fixed"
            ebike_speed_model = parameters[7].valueAsText or "Fixed"
            fixed_bike_speed = _safe_float(parameters[8].value, 15.0)
            fixed_ebike_speed = _safe_float(parameters[9].value, 25.0)
            max_bike_cap = _safe_float(parameters[10].value, 40.0)
            max_ebike_cap = _safe_float(parameters[11].value, 45.0)
            multipart_handling = parameters[12].valueAsText or "Warn"
            geometry_validation = parameters[13].valueAsText or "CheckOnly"
            included_path_types_raw = parameters[14].valueAsText or ""
            allow_non_polyline = _bool_value(parameters[15])
            write_qa_report = _bool_value(parameters[16])
            if_output_exists = parameters[17].valueAsText or "SuffixTimestamp"
            selected_path_types = _parse_path_type_selection(included_path_types_raw)

            dataset_slug = os.path.splitext(os.path.basename(local_geojson_path))[0] if local_geojson_path else ""
            resource_url = local_geojson_path

            qa["RunTimestamp"] = datetime.datetime.now().isoformat()
            qa["InputGeoJSON"] = local_geojson_path
            qa["DatasetSlug"] = dataset_slug
            qa["SelectedResourceURL"] = resource_url
            qa["EnrichmentMode"] = enrichment_mode
            qa["BikeSpeedModel"] = bike_speed_model
            qa["EbikeSpeedModel"] = ebike_speed_model
            qa["IncludedPathTypes"] = "ALL" if not selected_path_types else " | ".join(selected_path_types)

            # -----------------------------------------------------------------
            # Local input
            # -----------------------------------------------------------------
            arcpy.SetProgressorLabel("Step 1/7 - Validate local GeoJSON input")
            log("[1/7] Validating local input file...")

            if not local_geojson_path:
                raise RuntimeError("Input GeoJSON File is required.")
            if not os.path.isfile(local_geojson_path):
                raise RuntimeError("Input GeoJSON File does not exist: {}".format(local_geojson_path))
            if not os.path.isdir(output_folder):
                raise RuntimeError("Output folder does not exist: {}".format(output_folder))

            input_size = os.path.getsize(local_geojson_path)
            if input_size <= 0:
                raise RuntimeError("Input file is empty: {}".format(local_geojson_path))
            qa["InputSizeBytes"] = input_size

            if local_geojson_path.lower().endswith((".geojson", ".json")):
                if not _validate_download_signature(local_geojson_path):
                    warn("Input JSON/GeoJSON does not start with '{' or '[' after whitespace.")

            local_stage_dir = os.path.join(output_folder, "_bikedirect_local_input")
            if not os.path.isdir(local_stage_dir):
                os.makedirs(local_stage_dir)

            prepared_import_path = _prepare_download_for_import(local_geojson_path, local_stage_dir, True, log, warn)
            qa["PreparedImportPath"] = prepared_import_path

            log("Input file size: {} bytes".format(input_size))
            if prepared_import_path != local_geojson_path:
                log("Prepared import path: {}".format(prepared_import_path))
            arcpy.SetProgressorPosition()

            # -----------------------------------------------------------------
            # Output management (safe)
            # -----------------------------------------------------------------
            arcpy.SetProgressorLabel("Step 2/7 - Prepare output workspace")
            log("[2/7] Preparing output geodatabase and feature class path...")

            _ensure_output_gdb(output_gdb)
            output_fc, validated_fc_name = _resolve_output_fc(output_gdb, output_fc_name, if_output_exists)
            qa["OutputGDB"] = output_gdb
            qa["OutputFeatureClass"] = output_fc
            if validated_fc_name != output_fc_name:
                warn("Output name adjusted to valid GDB name: '{}'".format(validated_fc_name))

            arcpy.SetProgressorPosition()

            # -----------------------------------------------------------------
            # Import strategy
            # -----------------------------------------------------------------
            arcpy.SetProgressorLabel("Step 3/7 - Import GeoJSON/resource")
            log("[3/7] Import strategy: JSONToFeatures -> MakeFeatureLayer -> manual fallback")

            scratch_gdb = arcpy.env.scratchGDB
            if not scratch_gdb or not arcpy.Exists(scratch_gdb):
                scratch_gdb = output_gdb
                warn("scratchGDB unavailable; using output GDB for intermediates.")

            imported_fc = None
            import_method = None

            # 1) JSONToFeatures
            try:
                jtf_fc = arcpy.CreateUniqueName("bd_json_to_features", scratch_gdb)
                arcpy.conversion.JSONToFeatures(prepared_import_path, jtf_fc)
                if _count_rows(jtf_fc) > 0:
                    imported_fc = jtf_fc
                    import_method = "JSONToFeatures"
                    log("Import succeeded with JSONToFeatures.")
            except Exception as ex_json:
                warn("JSONToFeatures import failed: {}".format(ex_json))

            # 2) MakeFeatureLayer
            if imported_fc is None:
                lyr_name = "bd_geojson_lyr_{}".format(int(time.time()))
                try:
                    arcpy.management.MakeFeatureLayer(prepared_import_path, lyr_name)
                    mfl_fc = arcpy.CreateUniqueName("bd_makefeaturelayer", scratch_gdb)
                    arcpy.management.CopyFeatures(lyr_name, mfl_fc)
                    if _count_rows(mfl_fc) > 0:
                        imported_fc = mfl_fc
                        import_method = "MakeFeatureLayer"
                        log("Import succeeded with MakeFeatureLayer + CopyFeatures.")
                except Exception as ex_layer:
                    warn("MakeFeatureLayer import failed: {}".format(ex_layer))
                finally:
                    try:
                        if arcpy.Exists(lyr_name):
                            arcpy.management.Delete(lyr_name)
                    except Exception:
                        pass

            # 3) Manual fallback GeoJSON parser
            if imported_fc is None:
                if not prepared_import_path.lower().endswith((".geojson", ".json")):
                    raise RuntimeError(
                        "Auto import failed and fallback parser supports JSON/GeoJSON only. "
                        "Provide a local .geojson/.json file (or a ZIP containing one)."
                    )
                manual_fc, inserted, skipped_non_line, invalid_geom = _manual_geojson_to_fc(
                    prepared_import_path,
                    scratch_gdb,
                    allow_non_polyline,
                    log,
                    warn,
                )
                imported_fc = manual_fc
                import_method = "ManualGeoJSON"
                qa["ManualInserted"] = inserted
                qa["ManualSkippedNonPolyline"] = skipped_non_line
                qa["ManualInvalidGeometry"] = invalid_geom
                log("Import succeeded with manual GeoJSON parser.")

            if imported_fc is None:
                raise RuntimeError(
                    "All import methods failed for local input file '{}'. "
                    "Verify the GeoJSON content/encoding and ArcGIS readability.".format(prepared_import_path)
                )

            qa["ImportMethod"] = import_method
            arcpy.SetProgressorPosition()

            # -----------------------------------------------------------------
            # Sanity checks and geometry validation
            # -----------------------------------------------------------------
            arcpy.SetProgressorLabel("Step 4/7 - Sanity checks and geometry validation")
            log("[4/7] Running sanity checks and geometry validation...")

            pre_count = _count_rows(imported_fc)
            qa["FeatureCount_PreChecks"] = pre_count
            if pre_count <= 0:
                raise RuntimeError("Imported feature class has zero features.")

            desc = arcpy.Describe(imported_fc)
            shape_type = str(getattr(desc, "shapeType", ""))
            if shape_type.lower() != "polyline":
                if not allow_non_polyline:
                    raise RuntimeError(
                        "Imported geometry type is '{}' (not Polyline) and Allow Non-Polyline=False.".format(shape_type)
                    )
                raise RuntimeError(
                    "Imported data is '{}' geometry. Network-ready output requires polylines.".format(shape_type)
                )

            sr = getattr(desc, "spatialReference", None)
            if sr is None or sr.name in (None, "", "Unknown"):
                warn("Spatial reference is unknown after import.")
                assumptions.append("Unknown input spatial reference retained during import.")
                qa["SpatialReferenceName"] = "Unknown"
                qa["SpatialReferenceWKID"] = "Unknown"
            else:
                qa["SpatialReferenceName"] = sr.name
                qa["SpatialReferenceWKID"] = sr.factoryCode
                log("Input spatial reference: {} (WKID={})".format(sr.name, sr.factoryCode))

            ext = desc.extent
            qa["Extent_XMin"] = ext.XMin
            qa["Extent_YMin"] = ext.YMin
            qa["Extent_XMax"] = ext.XMax
            qa["Extent_YMax"] = ext.YMax
            log(
                "Extent: XMin={:.6f}, YMin={:.6f}, XMax={:.6f}, YMax={:.6f}".format(
                    ext.XMin,
                    ext.YMin,
                    ext.XMax,
                    ext.YMax,
                )
            )

            check_tbl = arcpy.CreateUniqueName("bd_check_geom", scratch_gdb)
            arcpy.management.CheckGeometry(imported_fc, check_tbl)
            geom_issues_pre = _count_rows(check_tbl)
            qa["GeometryIssues_PreRepair"] = geom_issues_pre
            if geom_issues_pre > 0:
                warn("Check Geometry found {} issues before repair.".format(geom_issues_pre))

            if geometry_validation == "RepairIfPossible" and geom_issues_pre > 0:
                arcpy.management.RepairGeometry(imported_fc, "DELETE_NULL")
                check_tbl_after = arcpy.CreateUniqueName("bd_check_geom_after", scratch_gdb)
                arcpy.management.CheckGeometry(imported_fc, check_tbl_after)
                geom_issues_post = _count_rows(check_tbl_after)
                qa["GeometryIssues_PostRepair"] = geom_issues_post
                if geom_issues_post > 0:
                    warn("Geometry issues remain after repair: {}".format(geom_issues_post))
                else:
                    log("Geometry repair completed with zero remaining issues.")
            else:
                qa["GeometryIssues_PostRepair"] = geom_issues_pre

            multipart_before = _multipart_count(imported_fc)
            qa["MultipartCount_Before"] = multipart_before
            working_fc = imported_fc

            if multipart_before > 0:
                if multipart_handling == "ExplodeToSinglepart":
                    single_fc = arcpy.CreateUniqueName("bd_singlepart", scratch_gdb)
                    arcpy.management.MultipartToSinglepart(imported_fc, single_fc)
                    working_fc = single_fc
                    log("MultipartToSinglepart created {}.".format(single_fc))
                else:
                    warn("Multipart features detected ({}) and retained (Warn mode).".format(multipart_before))

            qa["FeatureCount_PostSinglepart"] = _count_rows(working_fc)
            qa["MultipartCount_After"] = _multipart_count(working_fc)

            # Topology-like heuristic: endpoint fragmentation ratio
            try:
                frag_ratio, endpoint_count, unique_nodes = _endpoint_fragmentation_ratio(working_fc, scratch_gdb)
                qa["EndpointCount"] = endpoint_count
                qa["UniqueEndpointNodes"] = unique_nodes
                qa["EndpointUniqueRatio"] = round(frag_ratio, 6)
                if endpoint_count > 0 and frag_ratio > 0.70:
                    warn(
                        "Endpoint fragmentation appears high ({:.1f}% unique endpoints)."
                        " Network may be fragmented.".format(frag_ratio * 100.0)
                    )
            except Exception as ex_frag:
                warn("Endpoint fragmentation heuristic failed: {}".format(ex_frag))

            arcpy.SetProgressorPosition()

            # -----------------------------------------------------------------
            # Copy/project to final output FC
            # -----------------------------------------------------------------
            arcpy.SetProgressorLabel("Step 5/7 - Build output feature class")
            log("[5/7] Building final output feature class...")

            use_projected_lengths = False
            if target_sr:
                proj_fc = arcpy.CreateUniqueName("bd_projected", scratch_gdb)
                arcpy.management.Project(working_fc, proj_fc, target_sr)
                final_source_fc = proj_fc
                qa["ProjectedTo"] = getattr(target_sr, "name", "(unnamed SR)")
                target_type = str(getattr(target_sr, "type", ""))
                if target_type.lower() == "projected":
                    use_projected_lengths = True
                else:
                    warn(
                        "Target Spatial Reference is not projected. "
                        "Falling back to geodesic length calculation."
                    )
                    assumptions.append("Target SR was non-projected; used geodesic lengths.")
            else:
                final_source_fc = working_fc
                warn(
                    "Target Spatial Reference is blank. "
                    "Using geodesic lengths in meters; projected-meter workflow is recommended."
                )
                assumptions.append("Target Spatial Reference blank; used geodesic lengths.")

            qa["ProjectedLengthsUsed"] = "Yes" if use_projected_lengths else "No"

            arcpy.management.CopyFeatures(final_source_fc, output_fc)
            out_count = _count_rows(output_fc)
            qa["FeatureCount_Output"] = out_count
            if out_count <= 0:
                raise RuntimeError("Output feature class has zero features after copy.")

            out_desc = arcpy.Describe(output_fc)
            out_sr = getattr(out_desc, "spatialReference", None)
            qa["OutputSpatialReferenceName"] = getattr(out_sr, "name", "Unknown")
            qa["OutputSpatialReferenceWKID"] = getattr(out_sr, "factoryCode", "Unknown")

            arcpy.SetProgressorPosition()

            # -----------------------------------------------------------------
            # Enrichment
            # -----------------------------------------------------------------
            arcpy.SetProgressorLabel("Step 6/7 - Enrichment for bike/e-bike impedance")
            log("[6/7] Enriching output fields for bike/e-bike routing...")

            existing_fields = arcpy.ListFields(output_fc)
            class_candidates = ["class", "route_class", "bikeclass", "bikeway", "facility", "status", "route"]
            type_candidates = ["type", "path_type", "facility_type", "route_type"]
            one_way_candidates = ["one_way", "oneway", "one way", "direction", "dir", "from_to", "fromto"]

            class_field_src = _detect_field_name(existing_fields, class_candidates)
            class_source_for_clean = _detect_field_name(existing_fields, ["class", "route_class", "bikeclass"])
            type_field_src = _detect_field_name(existing_fields, type_candidates)
            one_way_field_src = _detect_field_name(existing_fields, one_way_candidates)

            if class_source_for_clean is None:
                class_source_for_clean = class_field_src

            if (bike_speed_model == "ClassBased" or ebike_speed_model == "ClassBased") and not class_field_src:
                warn("ClassBased speed model selected but no class-like field detected. Falling back to Fixed speeds.")
                assumptions.append("ClassBased speed fallback to Fixed due to missing class field.")

            if not type_field_src:
                warn("Type field not found. Type_Clean will be empty for all features.")
                assumptions.append("Type_Clean derived as empty because no type field was detected.")
            if not class_source_for_clean:
                warn("Class field not found. Class_Clean will be empty for all features.")
                assumptions.append("Class_Clean derived as empty because no class field was detected.")
            if not one_way_field_src:
                warn("one_way field not found; DirCode defaults to 0 (bidirectional).")
                assumptions.append("Directionality defaulted to bidirectional due to missing one_way field.")

            # Add/ensure enrichment fields
            _ensure_field(output_fc, "LenM", "DOUBLE")
            _ensure_field(output_fc, "LenKM", "DOUBLE")
            _ensure_field(output_fc, "SpeedKmh_Bike", "DOUBLE")
            _ensure_field(output_fc, "SpeedKmh_Ebike", "DOUBLE")
            _ensure_field(output_fc, "TimeMin_Bike", "DOUBLE")
            _ensure_field(output_fc, "TimeMin_Ebike", "DOUBLE")
            _ensure_field(output_fc, "BikeAccess", "SHORT")
            _ensure_field(output_fc, "EbikeAccess", "SHORT")
            _ensure_field(output_fc, "DirCode", "SHORT")
            _ensure_field(output_fc, "OneWay_Clean", "TEXT", 20)
            _ensure_field(output_fc, "Type_Clean", "TEXT", 120)
            _ensure_field(output_fc, "Class_Clean", "TEXT", 120)
            _ensure_field(output_fc, "PathTypeKey", "TEXT", 255)
            _ensure_field(output_fc, "Class", "TEXT", 120)
            _ensure_field(output_fc, "Source", "TEXT", 40)
            _ensure_field(output_fc, "LoadTS", "DATE")
            _ensure_field(output_fc, "CkanDataset", "TEXT", 120)
            _ensure_field(output_fc, "CkanResourceUrl", "TEXT", 500)
            _ensure_field(output_fc, "QA_Flag", "TEXT", 100)

            if enrichment_mode == "Strict":
                changed_vals, nullified_vals = _strict_trim_text_fields(output_fc)
                qa["StrictTrim_ChangedValues"] = changed_vals
                qa["StrictTrim_NullifiedEmptyStrings"] = nullified_vals
                log(
                    "Strict text hygiene applied: changed={}, nullified_empty={}".format(
                        changed_vals,
                        nullified_vals,
                    )
                )

            all_fields = arcpy.ListFields(output_fc)
            text_scan_fields = [f.name for f in all_fields if f.type == "String"]
            if len(text_scan_fields) > 30:
                text_scan_fields = sorted(text_scan_fields)[:30]
                assumptions.append("Access keyword scan limited to first 30 text fields for runtime control.")

            cursor_fields = [
                "SHAPE@",
                "LenM",
                "LenKM",
                "SpeedKmh_Bike",
                "SpeedKmh_Ebike",
                "TimeMin_Bike",
                "TimeMin_Ebike",
                "BikeAccess",
                "EbikeAccess",
                "DirCode",
                "OneWay_Clean",
                "Type_Clean",
                "Class_Clean",
                "PathTypeKey",
                "Class",
                "Source",
                "LoadTS",
                "CkanDataset",
                "CkanResourceUrl",
                "QA_Flag",
            ]

            if class_field_src and class_field_src not in cursor_fields:
                cursor_fields.append(class_field_src)
            if class_source_for_clean and class_source_for_clean not in cursor_fields:
                cursor_fields.append(class_source_for_clean)
            if type_field_src and type_field_src not in cursor_fields:
                cursor_fields.append(type_field_src)
            if one_way_field_src and one_way_field_src not in cursor_fields:
                cursor_fields.append(one_way_field_src)
            for tf in text_scan_fields:
                if tf not in cursor_fields:
                    cursor_fields.append(tf)

            idx = {fname: i for i, fname in enumerate(cursor_fields)}

            null_speed_bike = 0
            null_speed_ebike = 0
            null_time_bike = 0
            null_time_ebike = 0
            zero_len_count = 0
            access0_bike = 0
            access0_ebike = 0
            dircode_counts = {0: 0, 1: 0, 2: 0}
            normalized_22_count = 0
            unknown_one_way_values = set()
            available_path_type_keys = set()

            load_dt = datetime.datetime.now()
            total_rows = _count_rows(output_fc)
            arcpy.SetProgressor("step", "Enriching features...", 0, total_rows, 1)

            with arcpy.da.UpdateCursor(output_fc, cursor_fields) as ucur:
                for row_idx, row in enumerate(ucur, start=1):
                    geom = row[idx["SHAPE@"]]
                    if _geometry_is_empty(geom):
                        len_m = None
                    else:
                        try:
                            if use_projected_lengths:
                                len_m = float(geom.getLength("PLANAR", "METERS"))
                            else:
                                len_m = float(geom.getLength("GEODESIC", "METERS"))
                        except Exception:
                            try:
                                len_m = float(geom.length)
                            except Exception:
                                len_m = None

                    len_km = None
                    if len_m is not None:
                        len_m = max(len_m, 0.0)
                        len_km = len_m / 1000.0
                        if len_m == 0.0:
                            zero_len_count += 1

                    class_val = None
                    if class_field_src and class_field_src in idx:
                        class_val = row[idx[class_field_src]]
                    class_rule = _class_rule(class_val) if class_val is not None else None

                    type_clean = ""
                    if type_field_src and type_field_src in idx:
                        raw_type = row[idx[type_field_src]]
                        type_clean = str(raw_type).strip() if raw_type is not None else ""

                    class_clean = ""
                    if class_source_for_clean and class_source_for_clean in idx:
                        raw_class = row[idx[class_source_for_clean]]
                        class_clean = str(raw_class).strip() if raw_class is not None else ""

                    path_type_key = "{} | {}".format(type_clean, class_clean)
                    available_path_type_keys.add(path_type_key)

                    # Default access
                    bike_access = 1
                    ebike_access = 1

                    # Access based on text heuristics
                    text_blob_values = []
                    for tf in text_scan_fields:
                        v = row[idx[tf]] if tf in idx else None
                        if v is not None:
                            text_blob_values.append(str(v))
                    text_blob = " | ".join(text_blob_values)
                    txt_bike_access, txt_ebike_access = _blocked_access_from_text(text_blob)
                    bike_access = min(bike_access, txt_bike_access)
                    ebike_access = min(ebike_access, txt_ebike_access)

                    # Access from class rule
                    normalized_class = None
                    if class_rule:
                        normalized_class = class_rule.get("normalized")
                        bike_access = min(bike_access, int(class_rule.get("bike_access", 1)))
                        ebike_access = min(ebike_access, int(class_rule.get("ebike_access", 1)))
                    elif class_val is not None:
                        normalized_class = str(class_val).strip()[:120]

                    # Speeds
                    speed_bike = min(fixed_bike_speed, max_bike_cap)
                    speed_ebike = min(fixed_ebike_speed, max_ebike_cap)

                    if bike_speed_model == "ClassBased" and class_rule and class_rule.get("bike") is not None:
                        speed_bike = min(float(class_rule["bike"]), max_bike_cap)
                    elif bike_speed_model == "ClassBased" and not class_field_src:
                        # explicit fallback already warned
                        speed_bike = min(fixed_bike_speed, max_bike_cap)

                    if ebike_speed_model == "ClassBased" and class_rule and class_rule.get("ebike") is not None:
                        speed_ebike = min(float(class_rule["ebike"]), max_ebike_cap)
                    elif ebike_speed_model == "ClassBased" and not class_field_src:
                        speed_ebike = min(fixed_ebike_speed, max_ebike_cap)

                    # Directionality (one_way normalization)
                    if one_way_field_src and one_way_field_src in idx:
                        one_way_raw = row[idx[one_way_field_src]]
                        dir_code, one_way_clean, was_22, unknown_dir = _normalize_one_way_value(one_way_raw)
                        if was_22:
                            normalized_22_count += 1
                        if unknown_dir:
                            unknown_one_way_values.add(unknown_dir)
                    else:
                        one_way_clean = ""
                        dir_code = 0
                    dircode_counts[dir_code] = dircode_counts.get(dir_code, 0) + 1

                    # Times (minutes)
                    time_bike = None
                    time_ebike = None

                    if bike_access == 1:
                        if len_km is None:
                            time_bike = None
                        elif speed_bike is None or speed_bike <= 0:
                            time_bike = None
                            null_speed_bike += 1
                        elif len_km == 0:
                            time_bike = 0.0
                        else:
                            time_bike = (len_km / speed_bike) * 60.0
                    else:
                        time_bike = None

                    if ebike_access == 1:
                        if len_km is None:
                            time_ebike = None
                        elif speed_ebike is None or speed_ebike <= 0:
                            time_ebike = None
                            null_speed_ebike += 1
                        elif len_km == 0:
                            time_ebike = 0.0
                        else:
                            time_ebike = (len_km / speed_ebike) * 60.0
                    else:
                        time_ebike = None

                    if time_bike is None:
                        null_time_bike += 1
                    if time_ebike is None:
                        null_time_ebike += 1

                    if bike_access == 0:
                        access0_bike += 1
                    if ebike_access == 0:
                        access0_ebike += 1

                    # QA flag (stretch goal)
                    qa_flags = []
                    if len_m is None:
                        qa_flags.append("NO_GEOM")
                    elif len_m == 0:
                        qa_flags.append("ZERO_LEN")
                    elif len_m < 1.0:
                        qa_flags.append("VERY_SHORT")
                    elif len_m > 10000.0:
                        qa_flags.append("VERY_LONG")

                    row[idx["LenM"]] = len_m
                    row[idx["LenKM"]] = len_km
                    row[idx["SpeedKmh_Bike"]] = speed_bike
                    row[idx["SpeedKmh_Ebike"]] = speed_ebike
                    row[idx["TimeMin_Bike"]] = time_bike
                    row[idx["TimeMin_Ebike"]] = time_ebike
                    row[idx["BikeAccess"]] = bike_access
                    row[idx["EbikeAccess"]] = ebike_access
                    row[idx["DirCode"]] = dir_code
                    row[idx["OneWay_Clean"]] = one_way_clean[:20] if one_way_clean else ""
                    row[idx["Type_Clean"]] = type_clean[:120]
                    row[idx["Class_Clean"]] = class_clean[:120]
                    row[idx["PathTypeKey"]] = path_type_key[:255]
                    row[idx["Class"]] = (normalized_class[:120] if normalized_class else None)
                    row[idx["Source"]] = "BikeDirect"
                    row[idx["LoadTS"]] = load_dt
                    row[idx["CkanDataset"]] = dataset_slug[:120]
                    row[idx["CkanResourceUrl"]] = resource_url[:500]
                    row[idx["QA_Flag"]] = ";".join(qa_flags) if qa_flags else None

                    ucur.updateRow(row)

                    if row_idx % 5000 == 0:
                        log("Enrichment progress: {} / {}".format(row_idx, total_rows))
                    arcpy.SetProgressorPosition()

            qa["NullSpeed_Bike"] = null_speed_bike
            qa["NullSpeed_Ebike"] = null_speed_ebike
            qa["NullTime_Bike"] = null_time_bike
            qa["NullTime_Ebike"] = null_time_ebike
            qa["ZeroLengthCount"] = zero_len_count
            qa["BikeAccess0Count"] = access0_bike
            qa["EbikeAccess0Count"] = access0_ebike
            qa["DirectionalityInferred"] = "Yes" if one_way_field_src else "No"
            qa["DirCode0Count"] = dircode_counts.get(0, 0)
            qa["DirCode1Count"] = dircode_counts.get(1, 0)
            qa["DirCode2Count"] = dircode_counts.get(2, 0)
            qa["OneWay22NormalizedCount"] = normalized_22_count
            qa["OneWayUnknownValues"] = " | ".join(sorted(unknown_one_way_values)) if unknown_one_way_values else ""
            qa["AvailablePathTypeKeyCount"] = len(available_path_type_keys)
            qa["DetectedClassField"] = class_field_src or ""
            qa["DetectedDirectionField"] = one_way_field_src or ""
            qa["DetectedTypeField"] = type_field_src or ""

            available_keys_sorted = sorted(available_path_type_keys)
            if available_keys_sorted:
                preview_keys = available_keys_sorted[:40]
                preview_text = "; ".join(preview_keys)
                if len(available_keys_sorted) > len(preview_keys):
                    preview_text += " ... ({} total)".format(len(available_keys_sorted))
                log("Available PathTypeKey values: {}".format(preview_text))

            if normalized_22_count > 0:
                warn(
                    "one_way value '22' normalized to bidirectional (DirCode=0) for {} features.".format(
                        normalized_22_count
                    )
                )
            if unknown_one_way_values:
                warn(
                    "Unknown one_way values mapped to bidirectional (DirCode=0): {}".format(
                        ", ".join(sorted(unknown_one_way_values))
                    )
                )

            if selected_path_types:
                selected_valid = [v for v in selected_path_types if v in available_path_type_keys]
                selected_missing = [v for v in selected_path_types if v not in available_path_type_keys]
                if selected_missing:
                    warn(
                        "Included Path Types not found and ignored: {}".format(
                            "; ".join(selected_missing)
                        )
                    )
                if not selected_valid:
                    warn("No selected path types matched available PathTypeKey values; output will be empty.")
                    where_clause = "1=0"
                else:
                    where_clause = _sql_in_clause_text(output_fc, "PathTypeKey", selected_valid)
                filtered_fc = arcpy.CreateUniqueName("bd_path_filter", scratch_gdb)
                arcpy.analysis.Select(output_fc, filtered_fc, where_clause)
                filtered_count = _count_rows(filtered_fc)
                arcpy.management.Delete(output_fc)
                arcpy.management.CopyFeatures(filtered_fc, output_fc)
                qa["IncludedPathTypes"] = " | ".join(selected_valid) if selected_valid else "(none matched)"
                qa["FeatureCount_Output"] = filtered_count
                log(
                    "Applied PathTypeKey filter: {} requested, {} output features.".format(
                        len(selected_path_types),
                        filtered_count,
                    )
                )
            else:
                qa["IncludedPathTypes"] = "ALL"
                qa["FeatureCount_Output"] = _count_rows(output_fc)

            if zero_len_count > 0:
                warn("{} features have zero length; TimeMin values set to 0 where access allowed.".format(zero_len_count))
            if null_speed_bike > 0 or null_speed_ebike > 0:
                warn(
                    "Non-positive/null speeds encountered (Bike={}, E-bike={}); related time values set to NULL.".format(
                        null_speed_bike,
                        null_speed_ebike,
                    )
                )
            if null_time_bike > 0 or null_time_ebike > 0:
                warn(
                    "Null time values present (Bike={}, E-bike={})."
                    " Review access/speed/geometry assumptions.".format(null_time_bike, null_time_ebike)
                )

            arcpy.SetProgressorPosition()

            # -----------------------------------------------------------------
            # QA report
            # -----------------------------------------------------------------
            arcpy.SetProgressorLabel("Step 7/7 - QA report and summary")
            log("[7/7] Writing QA report and final summary...")

            qa_path = ""
            if write_qa_report:
                qa_rows = []
                # deterministic metric ordering
                preferred_order = [
                    "RunTimestamp",
                    "InputGeoJSON",
                    "DatasetSlug",
                    "SelectedResourceURL",
                    "InputSizeBytes",
                    "IncludedPathTypes",
                    "PreparedImportPath",
                    "ImportMethod",
                    "FeatureCount_PreChecks",
                    "FeatureCount_PostSinglepart",
                    "FeatureCount_Output",
                    "GeometryIssues_PreRepair",
                    "GeometryIssues_PostRepair",
                    "MultipartCount_Before",
                    "MultipartCount_After",
                    "EndpointCount",
                    "UniqueEndpointNodes",
                    "EndpointUniqueRatio",
                    "SpatialReferenceName",
                    "SpatialReferenceWKID",
                    "OutputSpatialReferenceName",
                    "OutputSpatialReferenceWKID",
                    "ProjectedLengthsUsed",
                    "Extent_XMin",
                    "Extent_YMin",
                    "Extent_XMax",
                    "Extent_YMax",
                    "NullSpeed_Bike",
                    "NullSpeed_Ebike",
                    "NullTime_Bike",
                    "NullTime_Ebike",
                    "ZeroLengthCount",
                    "BikeAccess0Count",
                    "EbikeAccess0Count",
                    "DirCode0Count",
                    "DirCode1Count",
                    "DirCode2Count",
                    "OneWay22NormalizedCount",
                    "OneWayUnknownValues",
                    "AvailablePathTypeKeyCount",
                    "DirectionalityInferred",
                    "DetectedClassField",
                    "DetectedTypeField",
                    "DetectedDirectionField",
                    "OutputGDB",
                    "OutputFeatureClass",
                    "EnrichmentMode",
                    "BikeSpeedModel",
                    "EbikeSpeedModel",
                ]

                seen = set()
                for key in preferred_order:
                    if key in qa:
                        qa_rows.append((key, qa[key]))
                        seen.add(key)

                for key in sorted(qa.keys()):
                    if key not in seen:
                        qa_rows.append((key, qa[key]))

                qa_rows.append(("WarningsCount", len(warnings_list)))
                if warnings_list:
                    qa_rows.append(("Warnings", " | ".join(warnings_list)))

                qa_rows.append(("AssumptionsCount", len(assumptions)))
                qa_rows.append(("Assumptions", " | ".join(assumptions) if assumptions else ""))

                elapsed = round(time.time() - start_time, 3)
                qa_rows.append(("ElapsedSeconds", elapsed))

                qa_base_name = "BikeDirect_QA_{}".format(_timestamp_compact())
                qa_path = _write_qa_csv_safe(output_folder, qa_base_name, qa_rows, warn)
                log("QA CSV written: {}".format(qa_path))
            else:
                qa["WriteQAReport"] = False

            elapsed_total = round(time.time() - start_time, 3)

            log("--- Run Summary ---")
            log("Input GeoJSON: {}".format(local_geojson_path))
            log("Output feature class: {}".format(output_fc))
            log("Features output: {}".format(_count_rows(output_fc)))
            log("Warnings: {}".format(len(warnings_list)))
            log("Elapsed seconds: {}".format(elapsed_total))

            log("Recommended Next Steps (Manual Network Dataset Test):")
            log("1) Create Network Dataset with edge source '{}'.".format(os.path.basename(output_fc)))
            log("2) Add cost attributes: BikeTime <- TimeMin_Bike, EbikeTime <- TimeMin_Ebike.")
            log("3) Set direction evaluator from DirCode: 1=FT only, 2=TF only, 0=both.")
            log("4) Build the network dataset.")
            log("5) Run Service Area twice (BikeTime and EbikeTime) with breaks 5/10/15 minutes.")
            log("6) Validate isochrones visually from a known origin point.")

            arcpy.SetParameterAsText(18, output_fc)
            arcpy.SetParameterAsText(19, qa_path)

        except Exception as ex:
            err("Tool failed: {}".format(ex))
            err("Input GeoJSON='{}'".format(parameters[0].valueAsText))
            err(traceback.format_exc())
            raise
        finally:
            arcpy.ResetProgressor()
class CreateOSMNetworkDatasetGDB(object):
    def __init__(self):
        self.label = "1.2 Transport - Create OSM_ND_GDB in Project Home"
        self.description = (
            "Create an empty OSM_ND_GDB file geodatabase in the current ArcGIS Pro "
            "project home folder for use by the OSM to Network Dataset tool."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="Created/Existing Geodatabase Path",
            name="created_existing_gdb_path",
            datatype="GPString",
            parameterType="Derived",
            direction="Output",
        )
        return [p0]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def _project_home_folder(self):
        try:
            aprx = arcpy.mp.ArcGISProject("CURRENT")
            home = getattr(aprx, "homeFolder", None)
            if home and os.path.isdir(home):
                return home
        except Exception:
            pass

        try:
            workspace = arcpy.env.workspace
            if workspace:
                if workspace.lower().endswith(".gdb"):
                    workspace = os.path.dirname(workspace)
                if os.path.isdir(workspace):
                    arcpy.AddWarning(
                        "Could not resolve the current ArcGIS Pro project home folder. "
                        "Using arcpy.env.workspace folder instead."
                    )
                    return workspace
        except Exception:
            pass

        fallback = os.getcwd()
        arcpy.AddWarning(
            "Could not resolve the current ArcGIS Pro project home folder or arcpy.env.workspace. "
            "Using the current working directory instead: {}".format(fallback)
        )
        return fallback

    def _count_gdb_contents(self, gdb_path):
        old_workspace = arcpy.env.workspace
        try:
            arcpy.env.workspace = gdb_path
            feature_datasets = arcpy.ListDatasets(feature_type="feature") or []
            feature_classes = arcpy.ListFeatureClasses() or []
            tables = arcpy.ListTables() or []
            rasters = arcpy.ListRasters() or []
            return {
                "feature_datasets": len(feature_datasets),
                "feature_classes": len(feature_classes),
                "tables": len(tables),
                "rasters": len(rasters),
                "total": len(feature_datasets) + len(feature_classes) + len(tables) + len(rasters),
            }
        finally:
            arcpy.env.workspace = old_workspace

    def execute(self, parameters, messages):
        arcpy.AddMessage("=" * 60)
        arcpy.AddMessage("Create OSM_ND_GDB File Geodatabase")
        arcpy.AddMessage("=" * 60)

        home_folder = self._project_home_folder()
        gdb_name = "OSM_ND_GDB"
        gdb_path = os.path.join(home_folder, gdb_name + ".gdb")

        arcpy.AddMessage("Project home folder: {}".format(home_folder))
        arcpy.AddMessage("Target geodatabase: {}".format(gdb_path))

        if arcpy.Exists(gdb_path):
            arcpy.AddWarning("OSM_ND_GDB.gdb already exists. No delete, overwrite, or cleanup action was performed.")
            counts = self._count_gdb_contents(gdb_path)
            if counts["total"] == 0:
                arcpy.AddMessage("Existing geodatabase is empty and ready for the OSM tool.")
            else:
                arcpy.AddWarning(
                    "Existing geodatabase is not empty: {} feature dataset(s), {} root feature class(es), "
                    "{} table(s), {} raster(s). The OSM to Network Dataset tool expects a completely empty geodatabase."
                    .format(
                        counts["feature_datasets"],
                        counts["feature_classes"],
                        counts["tables"],
                        counts["rasters"],
                    )
                )
        else:
            arcpy.management.CreateFileGDB(home_folder, gdb_name)
            arcpy.AddMessage("Created file geodatabase: {}".format(gdb_path))
            arcpy.AddMessage("OSM_ND_GDB.gdb is empty and ready for the OSM tool.")

        arcpy.SetParameterAsText(0, gdb_path)
        arcpy.AddMessage("=" * 60)
        return

class OSMToNetworkDataset(object):
    def __init__(self):
        """Define the tool (tool name is the name of the class)."""
        self.label = "1.3 Transport - OSM to Network Dataset (Preconfigured)"
        self.description = "Fetches OpenStreetMap road data and converts it to an ArcGIS Network Dataset with preconfigured attributes"
        self.canRunInBackground = False

    def getParameterInfo(self):
        """Define parameter definitions"""
        
        # Parameter 0: Output Geodatabase
        param0 = arcpy.Parameter(
            displayName="Output Geodatabase (!!THIS MUST BE COMPLETELY EMPTY!!)",
            name="output_geodatabase",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input")
        param0.filter.list = ["Local Database"]
        
        # Parameter 1: Extent Method
        param1 = arcpy.Parameter(
            displayName="Extent Method",
            name="extent_method",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        param1.filter.type = "ValueList"
        param1.filter.list = ["Use Bounding Box", "Use Feature Extent"]
        param1.value = "Use Feature Extent"
        
        # Parameter 2: Input Extent Feature
        param2 = arcpy.Parameter(
            displayName="Input Extent Feature",
            name="input_extent_feature",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input")
        
        # Parameter 3: Bounding Box South
        param3 = arcpy.Parameter(
            displayName="Bounding Box South (Latitude)",
            name="bbox_south",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        param3.value = -34.9459
        
        # Parameter 4: Bounding Box West
        param4 = arcpy.Parameter(
            displayName="Bounding Box West (Longitude)",
            name="bbox_west",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        param4.value = 138.5734
        
        # Parameter 5: Bounding Box North
        param5 = arcpy.Parameter(
            displayName="Bounding Box North (Latitude)",
            name="bbox_north",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        param5.value = -34.8951
        
        # Parameter 6: Bounding Box East
        param6 = arcpy.Parameter(
            displayName="Bounding Box East (Longitude)",
            name="bbox_east",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        param6.value = 138.6296
        
        # Parameter 7: Extent Buffer Percentage
        param7 = arcpy.Parameter(
            displayName="Extent Buffer Percentage",
            name="extent_buffer",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        param7.value = 0.0
        
        # Parameter 8: Feature Dataset Name
        param8 = arcpy.Parameter(
            displayName="Feature Dataset Name",
            name="feature_dataset_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        param8.value = "Transport_Network_FD"
        
        # Parameter 9: API Timeout
        param9 = arcpy.Parameter(
            displayName="API Timeout (seconds)",
            name="api_timeout",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input")
        param9.value = 180
        
        # Parameter 10: Output Network Dataset (derived)
        param10 = arcpy.Parameter(
            displayName="Output Network Dataset",
            name="output_network",
            datatype="DEFeatureDataset",
            parameterType="Derived",
            direction="Output")
        
        params = [param0, param1, param2, param3, param4, param5, param6, param7, param8, param9, param10]
        return params

    def isLicensed(self):
        """Set whether tool is licensed to execute."""
        return True

    def updateParameters(self, parameters):
        """Modify the values and properties of parameters before internal
        validation is performed. This method is called whenever a parameter
        has been changed."""
        
        # Enable/disable parameters based on extent method
        if parameters[1].value == "Use Feature Extent":
            parameters[2].enabled = True  # Input Extent Feature
            parameters[3].enabled = False  # Bbox South
            parameters[4].enabled = False  # Bbox West
            parameters[5].enabled = False  # Bbox North
            parameters[6].enabled = False  # Bbox East
        else:  # Use Bounding Box
            parameters[2].enabled = False  # Input Extent Feature
            parameters[3].enabled = True   # Bbox South
            parameters[4].enabled = True   # Bbox West
            parameters[5].enabled = True   # Bbox North
            parameters[6].enabled = True   # Bbox East
        
        return

    def updateMessages(self, parameters):
        """Modify the messages created by internal validation for each tool
        parameter. This method is called after internal validation."""
        
        # Validate extent method requirements
        if parameters[1].value == "Use Feature Extent":
            if not parameters[2].value:
                parameters[2].setErrorMessage("Input Extent Feature is required when using 'Use Feature Extent' method")
        else:  # Use Bounding Box
            if not all([parameters[3].value, parameters[4].value, parameters[5].value, parameters[6].value]):
                parameters[3].setErrorMessage("All bounding box coordinates are required when using 'Use Bounding Box' method")
        
        return

    def execute(self, parameters, messages):
        """The source code of the tool."""
        
        # Import libraries here to avoid load-time errors
        import os
        import requests
        import json
        import datetime
        import time
        
        # Get parameters. Always resolve the GDB to a stable absolute path.
        # ArcGIS Pro GP script processes can have different current directories;
        # a relative path such as .\\OSM_ND_GDB.gdb can fail during
        # CalculateGeometryAttributes even after earlier operations succeeded.
        supplied_workspace = parameters[0].valueAsText
        if not supplied_workspace:
            raise ValueError("An output file geodatabase is required.")
        supplied_workspace = os.path.expandvars(os.path.expanduser(supplied_workspace))
        if os.path.isabs(supplied_workspace):
            workspace = os.path.normpath(supplied_workspace)
        else:
            try:
                project_home = arcpy.mp.ArcGISProject("CURRENT").homeFolder
            except Exception:
                project_home = None
            if not project_home or not os.path.isdir(project_home):
                raise ValueError(
                    "Relative output geodatabase paths require a saved ArcGIS Pro project. "
                    "Save the project or select an absolute .gdb path."
                )
            workspace = os.path.abspath(os.path.join(project_home, supplied_workspace))
        if not workspace.lower().endswith(".gdb"):
            raise ValueError("Output path must end in .gdb: {}".format(workspace))
        arcpy.AddMessage("Resolved output geodatabase: {}".format(workspace))
        extent_method = parameters[1].valueAsText
        input_extent_feature = parameters[2].valueAsText
        bbox_south = parameters[3].value
        bbox_west = parameters[4].value
        bbox_north = parameters[5].value
        bbox_east = parameters[6].value
        extent_buffer = parameters[7].value if parameters[7].value is not None else 0.0
        output_feature_dataset = parameters[8].valueAsText if parameters[8].valueAsText else "Transport_Network_FD"
        timeout_seconds = parameters[9].value if parameters[9].value else 180
        
        # ============================================================================
        # DETERMINE BOUNDING BOX
        # ============================================================================
        
        arcpy.AddMessage("Determining extent for OSM data fetch...")
        
        if extent_method == "Use Feature Extent":
            # Get extent from input feature
            desc = arcpy.Describe(input_extent_feature)
            extent = desc.extent
            
            # Get spatial reference
            input_sr = desc.spatialReference
            wgs84_sr = arcpy.SpatialReference(4326)
            
            # Project extent to WGS84 if needed
            if input_sr.factoryCode != 4326:
                arcpy.AddMessage(f"Projecting extent from {input_sr.name} to WGS84...")
                extent_array = arcpy.Array([
                    arcpy.Point(extent.XMin, extent.YMin),
                    arcpy.Point(extent.XMin, extent.YMax),
                    arcpy.Point(extent.XMax, extent.YMax),
                    arcpy.Point(extent.XMax, extent.YMin)
                ])
                extent_polygon = arcpy.Polygon(extent_array, input_sr)
                extent_polygon_wgs84 = extent_polygon.projectAs(wgs84_sr)
                extent = extent_polygon_wgs84.extent
            
            bbox_south_val = extent.YMin
            bbox_west_val = extent.XMin
            bbox_north_val = extent.YMax
            bbox_east_val = extent.XMax
            
            arcpy.AddMessage(f"Extracted extent from feature: {input_extent_feature}")
            
        else:  # Use Bounding Box
            bbox_south_val = bbox_south
            bbox_west_val = bbox_west
            bbox_north_val = bbox_north
            bbox_east_val = bbox_east
            
            arcpy.AddMessage(f"Using provided bounding box coordinates")
        
        # Apply buffer
        if extent_buffer > 0:
            lat_range = bbox_north_val - bbox_south_val
            lon_range = bbox_east_val - bbox_west_val
            
            lat_buffer = lat_range * (extent_buffer / 100.0) / 2.0
            lon_buffer = lon_range * (extent_buffer / 100.0) / 2.0
            
            bbox_south_val -= lat_buffer
            bbox_north_val += lat_buffer
            bbox_west_val -= lon_buffer
            bbox_east_val += lon_buffer
            
            arcpy.AddMessage(f"Applied {extent_buffer}% buffer to extent")
        
        bbox = (bbox_south_val, bbox_west_val, bbox_north_val, bbox_east_val)
        arcpy.AddMessage(f"Final bounding box: South={bbox[0]:.6f}, West={bbox[1]:.6f}, North={bbox[2]:.6f}, East={bbox[3]:.6f}")
        
        # ============================================================================
        # CONFIGURATION
        # ============================================================================
        
        output_roads = "Roads_Network"
        output_footpaths = "Footpaths_Network"
        output_junctions = "Junctions"
        output_network = "Adelaide_Transport_ND"
        
        overpass_url = "https://overpass-api.de/api/interpreter"
        spatial_ref = arcpy.SpatialReference(8059)  # GDA2020 / South Australia Lambert: metres, statewide
        
        speed_limits = {
            'motorway': 110, 'trunk': 90, 'primary': 80, 'secondary': 70,
            'tertiary': 60, 'residential': 50, 'unclassified': 50, 'service': 40,
            'living_street': 40, 'track': 40, 'footway': 5, 'cycleway': 20,
            'path': 10, 'pedestrian': 5, 'steps': 2
        }
        
        road_hierarchy = {
            'motorway': 1, 'trunk': 2, 'primary': 3, 'secondary': 4,
            'tertiary': 5, 'residential': 6, 'unclassified': 7, 'service': 8,
            'living_street': 8, 'track': 9, 'footway': 10, 'cycleway': 10,
            'path': 10, 'pedestrian': 10, 'steps': 10
        }
        
        timestamp = str(int(time.time()))

        arcpy.AddMessage(f"Starting OSM to Network Dataset conversion - {datetime.datetime.now()}")

        # ============================================================================
        # PRE-FLIGHT: verify GDB and Geometry Attributes *before* downloading OSM.
        # ============================================================================
        if not arcpy.Exists(workspace):
            arcpy.AddMessage("Creating geodatabase: {}".format(workspace))
            gdb_parent = os.path.dirname(workspace)
            if not os.path.isdir(gdb_parent):
                os.makedirs(gdb_parent, exist_ok=True)
            arcpy.management.CreateFileGDB(gdb_parent, os.path.splitext(os.path.basename(workspace))[0])

        if not os.path.isdir(workspace):
            raise RuntimeError("Output geodatabase is missing or not a directory: {}".format(workspace))
        try:
            gdb_description = arcpy.Describe(workspace)
            if getattr(gdb_description, "workspaceType", None) != "LocalDatabase":
                raise RuntimeError("Output is not a local geodatabase: {}".format(workspace))
        except Exception as exc:
            raise RuntimeError(
                "Output geodatabase cannot be opened; replace any corrupted GDB before running: {} ({})"
                .format(workspace, exc)
            ) from exc

        arcpy.env.workspace = workspace
        arcpy.env.overwriteOutput = True
        # Do not silently mix new OSM results with any previous or damaged run.
        existing_items = (
            (arcpy.ListDatasets() or [])
            + (arcpy.ListFeatureClasses() or [])
            + (arcpy.ListTables() or [])
            + (arcpy.ListRasters() or [])
        )
        if existing_items:
            raise RuntimeError(
                "OSM tool requires a NEW, EMPTY geodatabase. Found existing items: {}. "
                "Create a new GDB instead of reusing a previous or corrupted one."
                .format(", ".join(existing_items[:10]))
            )

        # Probe the exact GP operation that failed after the previous 54-minute run.
        probe_name = "_osm_geometry_preflight_{}".format(timestamp)
        probe_path = os.path.join(workspace, probe_name)
        arcpy.AddMessage("Pre-flight: testing geodatabase write and CalculateGeometryAttributes...")
        try:
            arcpy.management.CreateFeatureclass(workspace, probe_name, "POLYLINE", spatial_reference=spatial_ref)
            arcpy.management.AddField(probe_path, "Length_KM", "DOUBLE")
            wgs84 = arcpy.SpatialReference(4326)
            p1 = arcpy.PointGeometry(arcpy.Point(138.6000, -34.9300), wgs84).projectAs(spatial_ref)
            p2 = arcpy.PointGeometry(arcpy.Point(138.6010, -34.9300), wgs84).projectAs(spatial_ref)
            test_line = arcpy.Polyline(
                arcpy.Array([p1.firstPoint, p2.firstPoint]), spatial_ref
            )
            with arcpy.da.InsertCursor(probe_path, ["SHAPE@"]) as probe_cursor:
                probe_cursor.insertRow((test_line,))
            arcpy.management.CalculateGeometryAttributes(
                probe_path, [["Length_KM", "LENGTH_GEODESIC"]], "KILOMETERS"
            )
            with arcpy.da.SearchCursor(probe_path, ["Length_KM"]) as probe_cursor:
                test_length = next(probe_cursor)[0]
            if test_length is None or not 0 < test_length < 1:
                raise RuntimeError("Geometry pre-flight returned invalid length: {}".format(test_length))
            arcpy.AddMessage("Pre-flight passed: {:.4f} km; WKID {}".format(test_length, spatial_ref.factoryCode))
        except Exception as exc:
            raise RuntimeError(
                "Geodatabase pre-flight failed BEFORE fetching OSM. "
                "Choose a new, local, writable .gdb using an absolute path: {}. Error: {}"
                .format(workspace, exc)
            ) from exc
        finally:
            if arcpy.Exists(probe_path):
                arcpy.management.Delete(probe_path)
        
        # ============================================================================
        # FETCH OSM DATA
        # ============================================================================
        
        arcpy.AddMessage("\n=== Fetching OSM Data ===")
        arcpy.SetProgressor("default", "Querying Overpass API...")
        
        overpass_query = f"""
        [out:json][timeout:{timeout_seconds}];
        (
          way["highway"]
              ["highway"!~"proposed|construction|raceway"]
              ({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        );
        out body;
        >;
        out skel qt;
        """
        
        overpass_headers = {
            "User-Agent": "UOS-ArcGISPro-Transport-OSM-Tool/1.1",
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        }

        try:
            arcpy.AddMessage(f"Overpass endpoint: {overpass_url}")
            arcpy.AddMessage(f"Overpass query length: {len(overpass_query)} characters")
            arcpy.AddMessage("Submitting Overpass request with explicit User-Agent and Accept headers...")

            response = requests.post(
                overpass_url,
                data={"data": overpass_query},
                headers=overpass_headers,
                timeout=timeout_seconds + 10
            )

            if response.status_code != 200:
                response_text = response.text or ""
                arcpy.AddError(f"Overpass API returned HTTP {response.status_code}")
                arcpy.AddError("This is an Overpass/API response, not an ArcGIS geodatabase error.")

                if response.status_code == 406:
                    arcpy.AddError("HTTP 406 usually means Overpass rejected the request representation or client/request policy. This patch adds explicit request headers, but the query may still be too large, blocked, or refused by the public endpoint.")
                elif response.status_code == 429:
                    arcpy.AddError("HTTP 429 means the Overpass endpoint is rate limiting the request. Wait before retrying or reduce the extent/buffer.")
                elif response.status_code in (502, 503, 504):
                    arcpy.AddError("The Overpass endpoint appears unavailable or overloaded. Retry later or reduce the extent/buffer.")

                if response_text.strip():
                    arcpy.AddError("Overpass response body, first 4000 characters:")
                    arcpy.AddError(response_text[:4000])

                return

            try:
                osm_data = response.json()
            except ValueError as json_error:
                arcpy.AddError(f"Overpass returned HTTP 200 but the response was not valid JSON: {json_error}")
                arcpy.AddError("Response body, first 4000 characters:")
                arcpy.AddError((response.text or "")[:4000])
                return

            arcpy.AddMessage(f"✓ Fetched {len(osm_data.get('elements', []))} OSM elements")
        except requests.exceptions.RequestException as e:
            arcpy.AddError(f"Failed to fetch OSM data from Overpass: {e}")
            return
        except Exception as e:
            arcpy.AddError(f"Failed to fetch OSM data: {e}")
            return
        
        # Parse OSM data
        arcpy.SetProgressor("default", "Processing OSM data...")
        nodes = {}
        ways = []
        
        for element in osm_data.get('elements', []):
            if element['type'] == 'node':
                nodes[element['id']] = (element['lon'], element['lat'])
            elif element['type'] == 'way':
                ways.append(element)
        
        # Split OSM ways at *shared OSM node IDs*, before writing any geometry.
        # Integrate with a metric tolerance is intentionally NOT used: it can
        # create false connections at overpasses or adjacent, unconnected paths.
        supported_highways = {
            'motorway', 'motorway_link', 'trunk', 'trunk_link',
            'primary', 'primary_link', 'secondary', 'secondary_link',
            'tertiary', 'tertiary_link', 'residential', 'unclassified',
            'service', 'living_street', 'track',
            'footway', 'path', 'pedestrian', 'steps',
        }
        features, retained_ways, missing_node_references = (
            _split_osm_ways_at_shared_nodes(ways, nodes, supported_highways)
        )
        arcpy.AddMessage(
            f"✓ Processed {retained_ways} OSM ways into "
            f"{len(features)} network edge parts at shared OSM nodes"
        )
        if missing_node_references:
            arcpy.AddWarning(
                f"{missing_node_references} OSM node references were missing. "
                "Affected portions were excluded, not bridged artificially."
            )
        if not features:
            raise RuntimeError(
                "No supported routable highway segments were returned by OSM. "
                "Check the extent and OSM tags."
            )

        # Create feature class
        arcpy.SetProgressor("default", "Creating feature class...")
        temp_roads = os.path.join(workspace, f"osm_roads_raw_{timestamp}")
        wgs84_sr = arcpy.SpatialReference(4326)
        
        arcpy.CreateFeatureclass_management(workspace, f"osm_roads_raw_{timestamp}", "POLYLINE", spatial_reference=wgs84_sr)
        
        osm_fields = [
            ('osm_id', 'TEXT', 50), ('highway', 'TEXT', 50), ('name', 'TEXT', 255),
            ('maxspeed', 'TEXT', 30), ('oneway', 'TEXT', 20), ('junction', 'TEXT', 30),
            ('bridge', 'TEXT', 30), ('tunnel', 'TEXT', 50), ('layer', 'TEXT', 20),
            ('surface', 'TEXT', 50), ('lanes', 'TEXT', 20), ('access', 'TEXT', 50),
            ('motor_vehicle', 'TEXT', 30)
        ]
        
        for field_name, field_type, field_length in osm_fields:
            arcpy.AddField_management(temp_roads, field_name, field_type, field_length=field_length)
        
        fields = ['SHAPE@'] + [f[0] for f in osm_fields]
        with arcpy.da.InsertCursor(temp_roads, fields) as cursor:
            for feature in features:
                array = arcpy.Array([arcpy.Point(x, y) for x, y in feature['geometry']])
                polyline = arcpy.Polyline(array, wgs84_sr)
                values = [polyline] + [feature.get(f[0])[:f[2]] if feature.get(f[0]) and isinstance(feature.get(f[0]), str) else feature.get(f[0]) for f in osm_fields]
                cursor.insertRow(values)
        
        # Project to target SR
        if wgs84_sr.factoryCode != spatial_ref.factoryCode:
            arcpy.SetProgressor("default", "Projecting to target coordinate system...")
            temp_roads_proj = temp_roads + "_proj"
            arcpy.Project_management(temp_roads, temp_roads_proj, spatial_ref)
            arcpy.Delete_management(temp_roads)
            arcpy.Rename_management(temp_roads_proj, temp_roads)
        
        # ============================================================================
        # DATA PREPARATION
        # ============================================================================
        
        arcpy.AddMessage("\n=== Data Preparation ===")
        arcpy.SetProgressor("default", "Filtering and cleaning data...")
        
        # Road types (motorized vehicles)
        highway_types_roads = ['motorway', 'motorway_link', 'trunk', 'trunk_link', 
                               'primary', 'primary_link', 'secondary', 'secondary_link',
                               'tertiary', 'tertiary_link', 'residential', 'unclassified',
                               'service', 'living_street', 'track']
        
        # Footpath types (pedestrian only)
        highway_types_footpaths = ['footway', 'path', 'pedestrian', 'steps']
        
        # Filter roads
        where_clause_roads = "highway IN ({})".format(','.join(["'{}'".format(h) for h in highway_types_roads]))
        temp_filtered_roads = os.path.join(workspace, f"temp_filtered_roads_{timestamp}")
        arcpy.Select_analysis(temp_roads, temp_filtered_roads, where_clause_roads)
        
        # Filter footpaths
        where_clause_footpaths = "highway IN ({})".format(','.join(["'{}'".format(h) for h in highway_types_footpaths]))
        temp_filtered_footpaths = os.path.join(workspace, f"temp_filtered_footpaths_{timestamp}")
        arcpy.Select_analysis(temp_roads, temp_filtered_footpaths, where_clause_footpaths)
        
        # Process roads
        arcpy.AddMessage("  Processing roads...")
        arcpy.RepairGeometry_management(temp_filtered_roads)
        temp_singlepart_roads = os.path.join(workspace, f"temp_single_roads_{timestamp}")
        arcpy.MultipartToSinglepart_management(temp_filtered_roads, temp_singlepart_roads)
        
        # Process footpaths
        arcpy.AddMessage("  Processing footpaths...")
        arcpy.RepairGeometry_management(temp_filtered_footpaths)
        temp_singlepart_footpaths = os.path.join(workspace, f"temp_single_footpaths_{timestamp}")
        arcpy.MultipartToSinglepart_management(temp_filtered_footpaths, temp_singlepart_footpaths)
        
        # ============================================================================
        # ADD NETWORK ATTRIBUTES
        # ============================================================================
        
        arcpy.AddMessage("\n=== Adding Network Attributes ===")
        arcpy.SetProgressor("default", "Adding network fields...")
        
        network_fields = [
            ('Road_Class', 'TEXT', None, 50), ('Speed_Limit', 'SHORT', None, None),
            ('OneWay', 'TEXT', None, 5), ('Travel_Time_Min', 'DOUBLE', None, None),
            ('Length_KM', 'DOUBLE', None, None), ('Hierarchy', 'SHORT', None, None),
            ('F_ZLEV', 'SHORT', None, None), ('T_ZLEV', 'SHORT', None, None),
            ('Surface', 'TEXT', None, 20), ('Lanes', 'SHORT', None, None),
            ('Restriction', 'TEXT', None, 50)
        ]
        
        # Add fields to roads
        arcpy.AddMessage("  Adding fields to roads...")
        existing_fields_roads = [f.name.upper() for f in arcpy.ListFields(temp_singlepart_roads)]
        for field_name, field_type, precision, length in network_fields:
            if field_name.upper() not in existing_fields_roads:
                if length:
                    arcpy.AddField_management(temp_singlepart_roads, field_name, field_type, field_length=length)
                else:
                    arcpy.AddField_management(temp_singlepart_roads, field_name, field_type)
        
        # Add fields to footpaths
        arcpy.AddMessage("  Adding fields to footpaths...")
        existing_fields_footpaths = [f.name.upper() for f in arcpy.ListFields(temp_singlepart_footpaths)]
        for field_name, field_type, precision, length in network_fields:
            if field_name.upper() not in existing_fields_footpaths:
                if length:
                    arcpy.AddField_management(temp_singlepart_footpaths, field_name, field_type, field_length=length)
                else:
                    arcpy.AddField_management(temp_singlepart_footpaths, field_name, field_type)
        
        # Calculate fields for ROADS
        arcpy.SetProgressor("default", "Calculating road attributes...")
        
        code_block_roads = f"""
def get_road_class(highway):
    if highway and highway.endswith('_link'):
        return highway.replace('_link', '')
    return highway if highway else 'unclassified'

def get_speed_limit(highway, maxspeed):
    speed_dict = {speed_limits}
    if maxspeed:
        try:
            speed_str = str(maxspeed).lower().replace('km/h', '').replace('kmh', '').strip()
            if speed_str.isdigit():
                return int(speed_str)
        except:
            pass
    road_class = get_road_class(highway)
    return speed_dict.get(road_class, 50)

def get_hierarchy(highway):
    hierarchy_dict = {road_hierarchy}
    road_class = get_road_class(highway)
    return hierarchy_dict.get(road_class, 10)
"""
        
        arcpy.CalculateField_management(temp_singlepart_roads, "Road_Class", "get_road_class(!highway!)", "PYTHON3", code_block_roads)
        arcpy.CalculateField_management(temp_singlepart_roads, "Speed_Limit", "get_speed_limit(!highway!, !maxspeed!)", "PYTHON3", code_block_roads)
        arcpy.CalculateField_management(temp_singlepart_roads, "Hierarchy", "get_hierarchy(!highway!)", "PYTHON3", code_block_roads)
        
        # Calculate fields for FOOTPATHS
        arcpy.SetProgressor("default", "Calculating footpath attributes...")
        
        footpath_code = """
def get_footpath_class(highway):
    return highway if highway else 'footway'

def get_walking_speed(highway):
    # Walking speeds in km/h
    speeds = {'footway': 5, 'path': 5, 'pedestrian': 4, 'steps': 2}
    return speeds.get(highway, 5)

def get_footpath_hierarchy(highway):
    # Higher numbers = lower priority (pedestrian routing)
    hierarchy = {'footway': 10, 'path': 10, 'pedestrian': 9, 'steps': 11}
    return hierarchy.get(highway, 10)
"""
        
        arcpy.CalculateField_management(temp_singlepart_footpaths, "Road_Class", "get_footpath_class(!highway!)", "PYTHON3", footpath_code)
        arcpy.CalculateField_management(temp_singlepart_footpaths, "Speed_Limit", "get_walking_speed(!highway!)", "PYTHON3", footpath_code)
        arcpy.CalculateField_management(temp_singlepart_footpaths, "Hierarchy", "get_footpath_hierarchy(!highway!)", "PYTHON3", footpath_code)
        
        # OneWay (applies to both)
        oneway_code = """
def get_oneway(oneway, junction):
    # OSM roundabouts are one-way by default unless explicitly overridden.
    if not oneway:
        return 'FT' if junction and str(junction).lower() == 'roundabout' else 'N'
    oneway_str = str(oneway).lower()
    if junction and str(junction).lower() == 'roundabout' and oneway_str not in ('no', '0', 'false'):
        return 'FT'
    if oneway_str in ['yes', '1', 'true']:
        return 'FT'
    elif oneway_str in ['-1', 'reverse']:
        return 'TF'
    else:
        return 'N'
"""
        arcpy.CalculateField_management(temp_singlepart_roads, "OneWay", "get_oneway(!oneway!, !junction!)", "PYTHON3", oneway_code)
        arcpy.CalculateField_management(temp_singlepart_footpaths, "OneWay", "get_oneway(!oneway!, !junction!)", "PYTHON3", oneway_code)
        
        # Z-levels (applies to both)
        zlev_code = """
def get_zlev(bridge, tunnel, layer):
    if bridge and str(bridge).lower() == 'yes':
        return 1
    elif tunnel and str(tunnel).lower() == 'yes':
        return -1
    elif layer:
        try:
            return int(layer)
        except:
            return 0
    return 0
"""
        arcpy.CalculateField_management(temp_singlepart_roads, "F_ZLEV", "get_zlev(!bridge!, !tunnel!, !layer!)", "PYTHON3", zlev_code)
        arcpy.CalculateField_management(temp_singlepart_roads, "T_ZLEV", "get_zlev(!bridge!, !tunnel!, !layer!)", "PYTHON3", zlev_code)
        arcpy.CalculateField_management(temp_singlepart_footpaths, "F_ZLEV", "get_zlev(!bridge!, !tunnel!, !layer!)", "PYTHON3", zlev_code)
        arcpy.CalculateField_management(temp_singlepart_footpaths, "T_ZLEV", "get_zlev(!bridge!, !tunnel!, !layer!)", "PYTHON3", zlev_code)
        
        # Surface, Lanes (simplified)
        arcpy.CalculateField_management(temp_singlepart_roads, "Surface", "'paved'", "PYTHON3")
        arcpy.CalculateField_management(temp_singlepart_roads, "Lanes", "1", "PYTHON3")
        arcpy.CalculateField_management(temp_singlepart_footpaths, "Surface", "'paved'", "PYTHON3")
        arcpy.CalculateField_management(temp_singlepart_footpaths, "Lanes", "1", "PYTHON3")
        
        # Geometry (both)
        arcpy.CalculateGeometryAttributes_management(temp_singlepart_roads, [["Length_KM", "LENGTH_GEODESIC"]], "KILOMETERS")
        arcpy.CalculateField_management(temp_singlepart_roads, "Travel_Time_Min", "(!Length_KM! / !Speed_Limit!) * 60", "PYTHON3")
        
        arcpy.CalculateGeometryAttributes_management(temp_singlepart_footpaths, [["Length_KM", "LENGTH_GEODESIC"]], "KILOMETERS")
        arcpy.CalculateField_management(temp_singlepart_footpaths, "Travel_Time_Min", "(!Length_KM! / !Speed_Limit!) * 60", "PYTHON3")
        
        # ============================================================================
        # TOPOLOGY / OSM-NODE CONNECTIVITY
        # ============================================================================

        arcpy.AddMessage("\n=== Ensuring Connectivity ===")
        arcpy.AddMessage(
            "Edge geometry is already split at shared OSM node IDs. "
            "Skipping Integrate to preserve true OSM connectivity and grade-separated crossings."
        )
        arcpy.AddMessage(
            "Road/footpath junctions will be created from their edge endpoints; "
            "validate the network's connectivity groups and elevation settings after creation."
        )
        
        # ============================================================================
        # CREATE NETWORK
        # ============================================================================
        
        arcpy.AddMessage("\n=== Creating Network Dataset ===")
        arcpy.SetProgressor("default", "Creating feature dataset...")
        
        if arcpy.Exists(os.path.join(workspace, output_feature_dataset)):
            arcpy.Delete_management(os.path.join(workspace, output_feature_dataset))
        
        arcpy.CreateFeatureDataset_management(workspace, output_feature_dataset, spatial_ref)
        
        # Copy roads
        output_roads_path = os.path.join(workspace, output_feature_dataset, output_roads)
        arcpy.CopyFeatures_management(temp_singlepart_roads, output_roads_path)
        
        # Copy footpaths
        output_footpaths_path = os.path.join(workspace, output_feature_dataset, output_footpaths)
        arcpy.CopyFeatures_management(temp_singlepart_footpaths, output_footpaths_path)
        
        # Create junctions from both roads and footpaths
        output_junctions_path = os.path.join(workspace, output_feature_dataset, output_junctions)
        
        # Create temp junction layers
        temp_junctions_roads = os.path.join(workspace, f"temp_junct_roads_{timestamp}")
        temp_junctions_footpaths = os.path.join(workspace, f"temp_junct_footpaths_{timestamp}")
        
        arcpy.FeatureVerticesToPoints_management(output_roads_path, temp_junctions_roads, "BOTH_ENDS")
        arcpy.FeatureVerticesToPoints_management(output_footpaths_path, temp_junctions_footpaths, "BOTH_ENDS")
        
        # Merge junctions
        arcpy.Merge_management([temp_junctions_roads, temp_junctions_footpaths], output_junctions_path)
        arcpy.DeleteIdentical_management(output_junctions_path, ["Shape"])
        
        # Try to create network dataset
        arcpy.SetProgressor("default", "Creating and configuring network dataset...")
        network_dataset_path = os.path.join(workspace, output_feature_dataset, output_network)
        
        try:
            arcpy.AddMessage("  Creating network dataset...")
            
            # Create network dataset using the geoprocessing tool with elevation
            arcpy.na.CreateNetworkDataset(
                feature_dataset=os.path.join(workspace, output_feature_dataset),
                out_name=output_network,
                source_feature_class_names=[output_roads, output_footpaths, output_junctions],
                elevation_model="ELEVATION_FIELDS"
            )
            
            arcpy.AddMessage("  ✓ Network dataset structure created (NOT BUILT OR CONFIGURED)")
            arcpy.AddMessage("  Note: Attributes need manual configuration")
            arcpy.AddMessage("  To complete setup:")
            arcpy.AddMessage("    1. Right-click network dataset > Properties")
            arcpy.AddMessage("    2. Verify/add these attributes:")
            arcpy.AddMessage("       - Travel_Time_Min (Cost, Minutes, use field)")
            arcpy.AddMessage("       - Length_KM (Cost, Kilometers, use field)")
            arcpy.AddMessage("       - OneWay (Restriction, use field)")
            arcpy.AddMessage("       - Hierarchy (Hierarchy, use field)")
            arcpy.AddMessage("    3. Set Elevation:")
            arcpy.AddMessage(f"       - {output_roads}: F_ZLEV (From) / T_ZLEV (To)")
            arcpy.AddMessage(f"       - {output_footpaths}: F_ZLEV (From) / T_ZLEV (To)")
            arcpy.AddMessage("    4. Click OK, then Build network")
            
            network_created = "partial"
            
        except Exception as e:
            arcpy.AddWarning(f"Network dataset creation failed: {e}")
            arcpy.AddWarning("Network creation geoprocessing messages: " + arcpy.GetMessages(2))
            arcpy.AddMessage("  Creating feature dataset only...")
            network_created = False
        
        # Clean up
        arcpy.Delete_management(temp_roads)
        arcpy.Delete_management(temp_filtered_roads)
        arcpy.Delete_management(temp_singlepart_roads)
        arcpy.Delete_management(temp_filtered_footpaths)
        arcpy.Delete_management(temp_singlepart_footpaths)
        arcpy.Delete_management(temp_junctions_roads)
        arcpy.Delete_management(temp_junctions_footpaths)
        
        # ============================================================================
        # COMPLETION
        # ============================================================================
        
        result = arcpy.GetCount_management(output_roads_path)
        arcpy.AddMessage(f"\n{'='*60}")
        arcpy.AddMessage("COMPLETE!")
        arcpy.AddMessage(f"{'='*60}")
        arcpy.AddMessage(f"Total road segments: {result[0]}")
        
        result = arcpy.GetCount_management(output_footpaths_path)
        arcpy.AddMessage(f"Total footpath segments: {result[0]}")
        
        result = arcpy.GetCount_management(output_junctions_path)
        arcpy.AddMessage(f"Total junctions: {result[0]}")
        
        arcpy.AddMessage(f"\nOutput: {os.path.join(workspace, output_feature_dataset)}")
        arcpy.AddMessage(f"  - {output_roads}")
        arcpy.AddMessage(f"  - {output_footpaths}")
        arcpy.AddMessage(f"  - {output_junctions}")
        
        if network_created == "partial":
            arcpy.AddMessage(f"  - {output_network} (needs configuration)")
            arcpy.AddMessage("\n" + "="*60)
            arcpy.AddMessage("Configure Network Dataset Attributes")
            arcpy.AddMessage("="*60)
            arcpy.AddMessage("1. Right-click network dataset > Properties")
            arcpy.AddMessage("2. Add Travel Attributes:")
            arcpy.AddMessage("   - Travel_Time_Min (Cost, Minutes, Field)")
            arcpy.AddMessage("   - Length_KM (Cost, Kilometers, Field)")
            arcpy.AddMessage("3. Add Restrictions:")
            arcpy.AddMessage("   - OneWay (Field: OneWay)")
            arcpy.AddMessage("   - Mode-specific access restrictions must be configured and validated")
            arcpy.AddMessage("4. Set Hierarchy: Field = Hierarchy")
            arcpy.AddMessage("5. Set Elevation:")
            arcpy.AddMessage(f"   - {output_roads}: F_ZLEV/T_ZLEV")
            arcpy.AddMessage(f"   - {output_footpaths}: F_ZLEV/T_ZLEV")
            arcpy.AddMessage("6. Click OK, then right-click > Build")
            
            # Set output to network dataset
            parameters[10].value = network_dataset_path
            
        else:
            arcpy.AddMessage("\n" + "="*60)
            arcpy.AddMessage("Manual Network Dataset Creation Required")
            arcpy.AddMessage("="*60)
            arcpy.AddMessage("1. In Catalog, right-click: Transport_Network_FD")
            arcpy.AddMessage("2. Select: New > Network Dataset")
            arcpy.AddMessage("3. Name: Adelaide_Transport_ND")
            arcpy.AddMessage(f"4. Sources: {output_roads}, {output_footpaths}, {output_junctions}")
            arcpy.AddMessage("5. Configure attributes as described above")
            
            # Set output to feature dataset
            parameters[10].value = os.path.join(workspace, output_feature_dataset)
        
        arcpy.AddMessage("="*60)
        arcpy.ResetProgressor()
        return


class AdelaideMetroRealtimePublicTransport(object):
    def __init__(self):
        self.label = "1.4 Transport - Adelaide Metro Realtime Public Transport"
        self.description = (
            "Fetch GTFS-Realtime public transport data from Adelaide Metro using a pure Python "
            "protobuf decoder (no external modules required), and write ArcGIS outputs."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="GTFS Realtime Base URL",
            name="gtfs_realtime_base_url",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p0.value = "https://gtfs.adelaidemetro.com.au/v1/realtime"

        p1 = arcpy.Parameter(
            displayName="Feeds to Fetch",
            name="feeds_to_fetch",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p1.multiValue = True
        p1.filter.type = "ValueList"
        p1.filter.list = ["vehicle_positions", "trip_updates", "service_alerts"]
        p1.value = "vehicle_positions;trip_updates;service_alerts"

        p2 = arcpy.Parameter(
            displayName="Output File Geodatabase",
            name="output_gdb",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input",
        )

        p3 = arcpy.Parameter(
            displayName="Output Name Prefix",
            name="output_name_prefix",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        p3.value = "AdelaideMetroRT"

        p4 = arcpy.Parameter(
            displayName="API Timeout (seconds)",
            name="api_timeout_seconds",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input",
        )
        p4.value = 30

        p5 = arcpy.Parameter(
            displayName="Max Retries",
            name="max_retries",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input",
        )
        p5.value = 3

        p6 = arcpy.Parameter(
            displayName="Validate Adelaide Bounds (Vehicle Positions)",
            name="validate_adelaide_bounds",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        p6.value = True

        p7 = arcpy.Parameter(
            displayName="If Output Exists",
            name="if_output_exists",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p7.filter.type = "ValueList"
        p7.filter.list = ["Fail", "Overwrite", "SuffixTimestamp"]
        p7.value = "SuffixTimestamp"

        p8 = arcpy.Parameter(
            displayName="Write Raw JSON Snapshot",
            name="write_raw_json_snapshot",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        p8.value = False

        p9 = arcpy.Parameter(
            displayName="Raw Snapshot Folder",
            name="raw_snapshot_folder",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input",
        )

        p10 = arcpy.Parameter(
            displayName="Apply VehicleType Symbology and RouteID Labels",
            name="apply_vehicle_type_symbology",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        p10.value = False

        p11 = arcpy.Parameter(
            displayName="Vehicle Positions Feature Class",
            name="vehicle_positions_fc",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output",
        )

        p12 = arcpy.Parameter(
            displayName="Trip Updates Table",
            name="trip_updates_table",
            datatype="DETable",
            parameterType="Derived",
            direction="Output",
        )

        p13 = arcpy.Parameter(
            displayName="Service Alerts Table",
            name="service_alerts_table",
            datatype="DETable",
            parameterType="Derived",
            direction="Output",
        )

        return [p0, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12, p13]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        write_snapshot = _bool_value(parameters[8])
        parameters[9].enabled = write_snapshot
        if not parameters[3].altered and not parameters[3].value:
            parameters[3].value = "AdelaideMetroRT"
        return

    def updateMessages(self, parameters):
        base_url = parameters[0].valueAsText
        output_gdb = parameters[2].valueAsText
        timeout_s = parameters[4].value
        retries = parameters[5].value
        write_snapshot = _bool_value(parameters[8])
        snapshot_folder = parameters[9].valueAsText

        if base_url and not base_url.lower().startswith(("http://", "https://")):
            parameters[0].setErrorMessage("GTFS Realtime Base URL must start with http:// or https://")

        if output_gdb and not output_gdb.lower().endswith(".gdb"):
            parameters[2].setErrorMessage("Output File Geodatabase must be a .gdb path.")

        if timeout_s is not None and int(timeout_s) <= 0:
            parameters[4].setErrorMessage("API Timeout must be greater than 0.")

        if retries is not None and int(retries) <= 0:
            parameters[5].setErrorMessage("Max Retries must be greater than 0.")

        if write_snapshot:
            if not snapshot_folder:
                parameters[9].setErrorMessage("Raw Snapshot Folder is required when Write Raw JSON Snapshot=True.")
            elif not os.path.isdir(snapshot_folder):
                parameters[9].setErrorMessage("Raw Snapshot Folder does not exist.")

        return

    def execute(self, parameters, messages):
        start_time = time.time()
        warnings_list = []

        def log(msg):
            arcpy.AddMessage(msg)

        def warn(msg):
            warnings_list.append(msg)
            arcpy.AddWarning(msg)

        def err(msg):
            arcpy.AddError(msg)

        try:
            base_url = (parameters[0].valueAsText or "").strip().rstrip("/")
            feeds_raw = parameters[1].valueAsText or ""
            output_gdb = parameters[2].valueAsText
            prefix = (parameters[3].valueAsText or "AdelaideMetroRT").strip()
            timeout_s = int(parameters[4].value) if parameters[4].value is not None else 30
            retries = int(parameters[5].value) if parameters[5].value is not None else 3
            validate_bounds = _bool_value(parameters[6])
            if_output_exists = parameters[7].valueAsText or "SuffixTimestamp"
            write_snapshot = _bool_value(parameters[8])
            snapshot_folder = parameters[9].valueAsText
            apply_vehicle_symbology = _bool_value(parameters[10])

            selected_feeds = [f.strip().lower() for f in feeds_raw.split(";") if f and f.strip()]
            if not selected_feeds:
                selected_feeds = ["vehicle_positions", "trip_updates", "service_alerts"]

            allowed = {"vehicle_positions", "trip_updates", "service_alerts"}
            invalid = [f for f in selected_feeds if f not in allowed]
            if invalid:
                raise RuntimeError("Invalid feed selection(s): {}".format(", ".join(invalid)))

            _ensure_output_gdb(output_gdb)

            arcpy.SetProgressor("step", "Adelaide Metro GTFS Realtime fetch", 0, len(selected_feeds) + 2, 1)
            arcpy.SetProgressorLabel("Preparing output datasets")
            log("Preparing output datasets...")

            vehicle_out = ""
            trip_out = ""
            alert_out = ""
            vehicle_temp = ""
            trip_temp = ""
            alert_temp = ""

            temp_workspace = "in_memory"
            try:
                _ = arcpy.Exists(temp_workspace)
            except Exception:
                temp_workspace = arcpy.env.scratchGDB if arcpy.env.scratchGDB else output_gdb
            if temp_workspace != "in_memory":
                warn("in_memory workspace unavailable; using '{}' for temp datasets.".format(temp_workspace))

            if "vehicle_positions" in selected_feeds:
                vehicle_out, _ = _resolve_output_fc(
                    output_gdb, "{}_VehiclePositions".format(prefix), if_output_exists
                )
                vehicle_temp_name = os.path.basename(arcpy.CreateUniqueName("{}_VehiclePositions_tmp".format(prefix), temp_workspace))
                vehicle_temp = arcpy.management.CreateFeatureclass(
                    temp_workspace, vehicle_temp_name, "POINT", spatial_reference=arcpy.SpatialReference(4326)
                )[0]
                for fld_name, fld_type, fld_len in [
                    ("FeedTimestamp", "DATE", None),
                    ("EntityID", "TEXT", 80),
                    ("VehicleID", "TEXT", 64),
                    ("VehicleLabel", "TEXT", 64),
                    ("LicensePlate", "TEXT", 64),
                    ("TripID", "TEXT", 64),
                    ("RouteID", "TEXT", 64),
                    ("DirectionID", "LONG", None),
                    ("StartTime", "TEXT", 16),
                    ("StartDate", "TEXT", 16),
                    ("Bearing", "DOUBLE", None),
                    ("Speed", "DOUBLE", None),
                    ("Odometer", "DOUBLE", None),
                    ("CurrentStopID", "TEXT", 64),
                    ("CurrentStopSeq", "LONG", None),
                    ("CurrentStatus", "LONG", None),
                    ("PositionTimestamp", "DATE", None),
                    ("VehicleType", "TEXT", 20),
                    ("FetchTS", "DATE", None),
                ]:
                    _ensure_field(vehicle_temp, fld_name, fld_type, fld_len)

            if "trip_updates" in selected_feeds:
                trip_out, _ = _resolve_output_fc(output_gdb, "{}_TripUpdates".format(prefix), if_output_exists)
                trip_temp_name = os.path.basename(arcpy.CreateUniqueName("{}_TripUpdates_tmp".format(prefix), temp_workspace))
                trip_temp = arcpy.management.CreateTable(temp_workspace, trip_temp_name)[0]
                for fld_name, fld_type, fld_len in [
                    ("FeedTimestamp", "DATE", None),
                    ("EntityID", "TEXT", 80),
                    ("TripID", "TEXT", 64),
                    ("RouteID", "TEXT", 64),
                    ("DirectionID", "LONG", None),
                    ("StartTime", "TEXT", 16),
                    ("StartDate", "TEXT", 16),
                    ("VehicleID", "TEXT", 64),
                    ("VehicleLabel", "TEXT", 64),
                    ("TripTimestamp", "DATE", None),
                    ("DelaySeconds", "LONG", None),
                    ("StopTimeUpdateCount", "LONG", None),
                    ("FirstStopID", "TEXT", 64),
                    ("FirstArrivalTime", "DATE", None),
                    ("FirstDepartureTime", "DATE", None),
                    ("FetchTS", "DATE", None),
                ]:
                    _ensure_field(trip_temp, fld_name, fld_type, fld_len)

            if "service_alerts" in selected_feeds:
                alert_out, _ = _resolve_output_fc(output_gdb, "{}_ServiceAlerts".format(prefix), if_output_exists)
                alert_temp_name = os.path.basename(arcpy.CreateUniqueName("{}_ServiceAlerts_tmp".format(prefix), temp_workspace))
                alert_temp = arcpy.management.CreateTable(temp_workspace, alert_temp_name)[0]
                for fld_name, fld_type, fld_len in [
                    ("FeedTimestamp", "DATE", None),
                    ("EntityID", "TEXT", 80),
                    ("Cause", "LONG", None),
                    ("Effect", "LONG", None),
                    ("Severity", "LONG", None),
                    ("HeaderText", "TEXT", 4000),
                    ("DescriptionText", "TEXT", 32000),
                    ("UrlText", "TEXT", 4000),
                    ("ActiveStart", "DATE", None),
                    ("ActiveEnd", "DATE", None),
                    ("InformedEntityCount", "LONG", None),
                    ("RouteIDs", "TEXT", 2000),
                    ("StopIDs", "TEXT", 2000),
                    ("TripIDs", "TEXT", 2000),
                    ("FetchTS", "DATE", None),
                ]:
                    _ensure_field(alert_temp, fld_name, fld_type, fld_len)

            arcpy.SetProgressorPosition()

            feed_counts = {}
            fetch_ts = datetime.datetime.utcnow()

            for feed_name in selected_feeds:
                endpoint = "{}/{}".format(base_url, feed_name)
                arcpy.SetProgressorLabel("Fetching {}".format(feed_name))
                log("Fetching {} from {}".format(feed_name, endpoint))
                payload = _fetch_bytes_with_retry(endpoint, retries=retries, timeout=timeout_s)
                log("Downloaded {} bytes for {}".format(len(payload), feed_name))

                if feed_name == "vehicle_positions":
                    records, header_ts = _gtfs_parse_vehicle_positions(payload, enforce_adelaide_bounds=validate_bounds)
                    feed_counts[feed_name] = len(records)
                    insert_fields = [
                        "SHAPE@XY",
                        "FeedTimestamp",
                        "EntityID",
                        "VehicleID",
                        "VehicleLabel",
                        "LicensePlate",
                        "TripID",
                        "RouteID",
                        "DirectionID",
                        "StartTime",
                        "StartDate",
                        "Bearing",
                        "Speed",
                        "Odometer",
                        "CurrentStopID",
                        "CurrentStopSeq",
                        "CurrentStatus",
                        "PositionTimestamp",
                        "VehicleType",
                        "FetchTS",
                    ]
                    with arcpy.da.InsertCursor(vehicle_temp, insert_fields) as icur:
                        for rec in records:
                            icur.insertRow(
                                [
                                    (rec.get("Longitude"), rec.get("Latitude")),
                                    rec.get("FeedTimestamp") or header_ts,
                                    rec.get("EntityID"),
                                    rec.get("VehicleID"),
                                    rec.get("VehicleLabel"),
                                    rec.get("LicensePlate"),
                                    rec.get("TripID"),
                                    rec.get("RouteID"),
                                    rec.get("DirectionID"),
                                    rec.get("StartTime"),
                                    rec.get("StartDate"),
                                    rec.get("Bearing"),
                                    rec.get("Speed"),
                                    rec.get("Odometer"),
                                    rec.get("CurrentStopID"),
                                    rec.get("CurrentStopSeq"),
                                    rec.get("CurrentStatus"),
                                    rec.get("PositionTimestamp"),
                                    rec.get("VehicleType"),
                                    fetch_ts,
                                ]
                            )
                    log("Inserted {} vehicle position records.".format(len(records)))

                elif feed_name == "trip_updates":
                    records, header_ts = _gtfs_parse_trip_updates(payload)
                    feed_counts[feed_name] = len(records)
                    insert_fields = [
                        "FeedTimestamp",
                        "EntityID",
                        "TripID",
                        "RouteID",
                        "DirectionID",
                        "StartTime",
                        "StartDate",
                        "VehicleID",
                        "VehicleLabel",
                        "TripTimestamp",
                        "DelaySeconds",
                        "StopTimeUpdateCount",
                        "FirstStopID",
                        "FirstArrivalTime",
                        "FirstDepartureTime",
                        "FetchTS",
                    ]
                    with arcpy.da.InsertCursor(trip_temp, insert_fields) as icur:
                        for rec in records:
                            icur.insertRow(
                                [
                                    rec.get("FeedTimestamp") or header_ts,
                                    rec.get("EntityID"),
                                    rec.get("TripID"),
                                    rec.get("RouteID"),
                                    rec.get("DirectionID"),
                                    rec.get("StartTime"),
                                    rec.get("StartDate"),
                                    rec.get("VehicleID"),
                                    rec.get("VehicleLabel"),
                                    rec.get("TripTimestamp"),
                                    rec.get("DelaySeconds"),
                                    rec.get("StopTimeUpdateCount"),
                                    rec.get("FirstStopID"),
                                    rec.get("FirstArrivalTime"),
                                    rec.get("FirstDepartureTime"),
                                    fetch_ts,
                                ]
                            )
                    log("Inserted {} trip update records.".format(len(records)))

                elif feed_name == "service_alerts":
                    records, header_ts = _gtfs_parse_service_alerts(payload)
                    feed_counts[feed_name] = len(records)
                    insert_fields = [
                        "FeedTimestamp",
                        "EntityID",
                        "Cause",
                        "Effect",
                        "Severity",
                        "HeaderText",
                        "DescriptionText",
                        "UrlText",
                        "ActiveStart",
                        "ActiveEnd",
                        "InformedEntityCount",
                        "RouteIDs",
                        "StopIDs",
                        "TripIDs",
                        "FetchTS",
                    ]
                    with arcpy.da.InsertCursor(alert_temp, insert_fields) as icur:
                        for rec in records:
                            icur.insertRow(
                                [
                                    rec.get("FeedTimestamp") or header_ts,
                                    _clip_text(rec.get("EntityID"), 80),
                                    rec.get("Cause"),
                                    rec.get("Effect"),
                                    rec.get("Severity"),
                                    _clip_text(rec.get("HeaderText"), 4000),
                                    _clip_text(rec.get("DescriptionText"), 32000),
                                    _clip_text(rec.get("UrlText"), 4000),
                                    rec.get("ActiveStart"),
                                    rec.get("ActiveEnd"),
                                    rec.get("InformedEntityCount"),
                                    _clip_text(rec.get("RouteIDs"), 2000),
                                    _clip_text(rec.get("StopIDs"), 2000),
                                    _clip_text(rec.get("TripIDs"), 2000),
                                    fetch_ts,
                                ]
                            )
                    log("Inserted {} service alert records.".format(len(records)))

                if write_snapshot and snapshot_folder:
                    snapshot_name = "AdelaideMetro_{}_{}.json".format(feed_name, _timestamp_compact())
                    snapshot_path = os.path.join(snapshot_folder, snapshot_name)
                    preview = records[:20] if "records" in locals() else []
                    with open(snapshot_path, "w", encoding="utf-8") as fobj:
                        json.dump(
                            {
                                "feed": feed_name,
                                "endpoint": endpoint,
                                "fetched_utc": fetch_ts.isoformat(),
                                "record_count": feed_counts.get(feed_name, 0),
                                "sample": preview,
                            },
                            fobj,
                            indent=2,
                            default=str,
                        )
                    log("Wrote raw JSON snapshot: {}".format(snapshot_path))

                arcpy.SetProgressorPosition()

            arcpy.SetProgressorLabel("Finalizing output datasets")
            if vehicle_temp and vehicle_out:
                arcpy.management.CopyFeatures(vehicle_temp, vehicle_out)
            if trip_temp and trip_out:
                arcpy.management.CopyRows(trip_temp, trip_out)
            if alert_temp and alert_out:
                arcpy.management.CopyRows(alert_temp, alert_out)

            if apply_vehicle_symbology:
                if vehicle_out:
                    arcpy.SetProgressorLabel("Applying VehicleType symbology and RouteID labels")
                    _apply_gtfs_vehicle_map_styling(vehicle_out, log, warn)
                else:
                    warn("VehicleType symbology was requested, but vehicle_positions was not selected.")

            elapsed = round(time.time() - start_time, 3)
            log("--- Adelaide Metro Realtime Summary ---")
            for k in selected_feeds:
                log("{}: {} records".format(k, feed_counts.get(k, 0)))
            log("Vehicle Positions FC: {}".format(vehicle_out if vehicle_out else "(not requested)"))
            log("Trip Updates Table: {}".format(trip_out if trip_out else "(not requested)"))
            log("Service Alerts Table: {}".format(alert_out if alert_out else "(not requested)"))
            log("Warnings: {}".format(len(warnings_list)))
            log("Elapsed seconds: {}".format(elapsed))

            arcpy.SetParameterAsText(11, vehicle_out)
            arcpy.SetParameterAsText(12, trip_out)
            arcpy.SetParameterAsText(13, alert_out)

        except Exception as ex:
            err("Tool failed: {}".format(ex))
            err(traceback.format_exc())
            raise
        finally:
            arcpy.ResetProgressor()
