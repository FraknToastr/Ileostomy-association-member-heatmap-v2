#!/usr/bin/env python3
"""Prepare anonymous suburb membership counts using only the Python standard library.

Read every sheet whose first populated row contains Residential Suburb (or select
one with --sheet). Count each following row, including repeated member values;
missing suburb cells and omitted interior rows are blankRows. Names are NFKC
normalized, whitespace collapsed and casefolded. No fuzzy matching is performed.

Aliases map source names to a canonical suburb name or an explicit suburb-N ID.
membershipMatch is exact, alias, mixed (both), or none (zero members). Reports
contain normalized unresolved names and aggregate counts, never member rows.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import posixpath
import re
import tempfile
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


ROOT = Path(__file__).resolve().parent.parent
HEADER = "residential suburb"


def normalize_name(value: object) -> str:
    """Make a comparison key without removing accents or guessing spelling."""
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def clean_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(element: ET.Element, name: str) -> ET.Element | None:
    return next((node for node in element if local_name(node.tag) == name), None)


def rich_text(element: ET.Element | None) -> str:
    """Combine plain/rich text runs, excluding phonetic annotations."""
    if element is None:
        return ""
    parts = []
    for node in element:
        if local_name(node.tag) == "t":
            parts.append(node.text or "")
        elif local_name(node.tag) == "r":
            text = child(node, "t")
            parts.append(text.text or "" if text is not None else "")
    return "".join(parts)


def cell_value(cell: ET.Element | None, shared: list[str]) -> str:
    if cell is None:
        return ""
    if cell.get("t") == "inlineStr":
        return rich_text(child(cell, "is"))
    value = child(cell, "v")
    text = value.text or "" if value is not None else ""
    if cell.get("t") == "s":
        try:
            index = int(text)
            if index < 0:
                raise ValueError
            return shared[index]
        except (ValueError, IndexError) as error:
            raise ValueError("Workbook contains an invalid shared-string reference") from error
    return text


def row_cells(row: ET.Element) -> dict[int, ET.Element]:
    cells = {}
    next_column = 0
    for cell in row:
        if local_name(cell.tag) != "c":
            continue
        reference = cell.get("r")
        if reference:
            match = re.fullmatch(r"([A-Za-z]+)[1-9][0-9]*", reference)
            if not match:
                raise ValueError("Workbook contains an invalid cell reference")
            column = 0
            for letter in match[1].upper():
                column = column * 26 + ord(letter) - ord("A") + 1
            column -= 1
        else:
            column = next_column
        if column in cells:
            raise ValueError("Workbook contains duplicate cells")
        cells[column] = cell
        next_column = column + 1
    return cells


def workbook_counts(path: Path, sheet_name: str | None = None) -> tuple[Counter, int, int]:
    """Return name counts, data rows and blank rows; do not retain member records."""
    counts = Counter()
    source_rows = blank_rows = matched_sheets = 0
    with ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationships = {}
        shared_path = None
        for rel in rels:
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            relationships[rel.get("Id")] = target
            if rel.get("Type", "").endswith("/sharedStrings"):
                shared_path = target
        shared = []
        if shared_path is not None:
            shared = [rich_text(node) for node in ET.fromstring(archive.read(shared_path)) if local_name(node.tag) == "si"]
        sheets = [node for node in workbook.iter() if local_name(node.tag) == "sheet"]
        if sheet_name is not None:
            sheets = [node for node in sheets if node.get("name") == sheet_name]
            if not sheets:
                raise ValueError("Requested workbook sheet does not exist")
        for sheet in sheets:
            relationship = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), None)
            sheet_path = relationships.get(relationship)
            if sheet_path is None:
                raise ValueError("Workbook sheet has no internal relationship")
            header_column = None
            previous_row = 0
            with archive.open(sheet_path) as stream:
                for _, row in ET.iterparse(stream, events=("end",)):
                    if local_name(row.tag) != "row":
                        continue
                    row_number = int(row.get("r", previous_row + 1))
                    if row_number <= previous_row:
                        raise ValueError("Workbook rows must be in increasing order")
                    cells = row_cells(row)
                    if header_column is None:
                        values = {column: normalize_name(cell_value(cell, shared)) for column, cell in cells.items()}
                        headers = [column for column, value in values.items() if value == HEADER]
                        if len(headers) > 1:
                            raise ValueError("Workbook sheet has duplicate Residential Suburb columns")
                        if headers:
                            header_column = headers[0]
                            matched_sheets += 1
                        elif any(values.values()):
                            break
                    else:
                        gap = row_number - previous_row
                        source_rows += gap
                        blank_rows += gap - 1
                        cell = cells.get(header_column)
                        name = normalize_name(cell_value(cell, shared))
                        if cell is not None and child(cell, "f") is not None and not name:
                            raise ValueError("Residential Suburb formula has no cached value; save calculated values first")
                        if cell is not None and cell.get("t") == "e":
                            raise ValueError("Residential Suburb contains an Excel error value")
                        if name:
                            counts[name] += 1
                        else:
                            blank_rows += 1
                    previous_row = row_number
                    row.clear()
    if not matched_sheets:
        raise ValueError("No sheet has a Residential Suburb header in its first populated row")
    return counts, source_rows, blank_rows


def suburb_features(data: dict) -> tuple[list[dict], dict, dict]:
    if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise ValueError("Suburbs must be a GeoJSON FeatureCollection")
    features = []
    by_name = defaultdict(list)
    by_id = {}
    for source in data["features"]:
        properties = source.get("properties") or {}
        object_id = properties.get("OBJECTID")
        if isinstance(object_id, bool) or not re.fullmatch(r"[0-9]+", str(object_id)):
            raise ValueError("Each suburb must have a nonnegative integer OBJECTID")
        identifier = f"suburb-{int(object_id)}"
        if identifier in by_id:
            raise ValueError("Suburb OBJECTID values must be unique")
        name = properties.get("suburb")
        if not isinstance(name, str) or not normalize_name(name):
            raise ValueError("Each suburb must have a nonblank suburb name")
        geometry = source.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not isinstance(coordinates, list) or len(coordinates) < 2:
            raise ValueError("Each suburb must have a Point geometry")
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in coordinates):
            raise ValueError("Suburb coordinates must be finite numbers")
        if not (-180 <= coordinates[0] <= 180 and -90 <= coordinates[1] <= 90):
            raise ValueError("Suburb coordinates must be longitude/latitude")
        postcode = properties.get("postcode")
        if postcode is None:
            postcode = ""
        elif isinstance(postcode, int) and not isinstance(postcode, bool):
            postcode = str(postcode).zfill(4)
        elif isinstance(postcode, str):
            postcode = clean_text(postcode)
        else:
            raise ValueError("Suburb postcodes must be strings or integers")
        feature = {
            "type": "Feature",
            "id": identifier,
            "geometry": {"type": "Point", "coordinates": list(coordinates)},
            "properties": {"id": identifier, "name": clean_text(name), "postcode": postcode, "members": 0, "membershipMatch": "none"},
        }
        features.append(feature)
        by_name[normalize_name(name)].append(feature)
        by_id[identifier] = feature
    features.sort(key=lambda feature: int(feature["id"].removeprefix("suburb-")))
    return features, by_name, by_id


def alias_targets(aliases: dict, by_name: dict, by_id: dict) -> dict:
    if not isinstance(aliases, dict):
        raise ValueError("Aliases must be a JSON object mapping names to canonical names or suburb IDs")
    targets = {}
    for source, target in aliases.items():
        if not isinstance(source, str) or not normalize_name(source) or not isinstance(target, str) or not normalize_name(target):
            raise ValueError("Alias names and targets must be nonblank strings")
        target_key = normalize_name(target)
        candidates = [by_id[target_key]] if target_key in by_id else by_name.get(target_key, [])
        if not candidates:
            raise ValueError(f"Alias target is absent from the suburb source: {clean_text(target)}")
        key = normalize_name(source)
        if key in targets and [f["id"] for f in targets[key]] != [f["id"] for f in candidates]:
            raise ValueError("Conflicting aliases normalize to the same source name")
        targets[key] = candidates
    return targets


def prepare_members(workbook: Path, suburbs: Path, aliases: Path, sheet_name: str | None = None) -> tuple[dict, dict]:
    counts, source_rows, blank_rows = workbook_counts(workbook, sheet_name)
    features, by_name, by_id = suburb_features(json.loads(suburbs.read_text(encoding="utf-8")))
    targets = alias_targets(json.loads(aliases.read_text(encoding="utf-8")), by_name, by_id)
    match_types = defaultdict(set)
    unresolved = []
    unmatched_members = ambiguous_members = 0
    for name, count in sorted(counts.items()):
        is_alias = name in targets
        candidates = targets[name] if is_alias else by_name.get(name, [])
        if len(candidates) == 1:
            feature = candidates[0]
            feature["properties"]["members"] += count
            match_types[feature["id"]].add("alias" if is_alias else "exact")
        elif not candidates:
            unmatched_members += count
            unresolved.append({"name": name, "count": count, "reason": "unmatched"})
        else:
            ambiguous_members += count
            unresolved.append({
                "name": name,
                "count": count,
                "reason": "ambiguous",
                "candidates": [{k: feature["properties"][k] for k in ("id", "name", "postcode")} for feature in sorted(candidates, key=lambda f: f["id"])],
            })
    for feature in features:
        kinds = match_types[feature["id"]]
        feature["properties"]["membershipMatch"] = "mixed" if len(kinds) > 1 else next(iter(kinds), "none")
    member_records = sum(counts.values())
    joined_members = sum(feature["properties"]["members"] for feature in features)
    unresolved_members = unmatched_members + ambiguous_members
    totals = {
        "sourceRows": source_rows,
        "memberRecords": member_records,
        "joinedMembers": joined_members,
        "unresolvedMembers": unresolved_members,
        "blankRows": blank_rows,
        "unmatchedMembers": unmatched_members,
        "ambiguousMembers": ambiguous_members,
    }
    if source_rows != member_records + blank_rows or member_records != joined_members + unresolved_members or unresolved_members != sum(item["count"] for item in unresolved):
        raise ValueError("Membership totals do not reconcile")
    return {"type": "FeatureCollection", "features": features}, {"totals": totals, "unresolved": unresolved}


def write_outputs(geojson: dict, report: dict, output_dir: Path, sources: tuple[Path, ...]) -> None:
    outputs = [(output_dir / "members.geojson", geojson), (output_dir / "membership-report.json", report)]
    source_paths = {source.resolve() for source in sources}
    if any(path.resolve() in source_paths for path, _ in outputs):
        raise ValueError("Output paths must not overwrite source files")
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, data in outputs:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_dir, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", "--workbook", "--members", dest="workbook", type=Path, default=ROOT / "Heat Map Data October 2026.xlsx", help="Source XLSX workbook")
    parser.add_argument("--suburbs", type=Path, default=ROOT / "Suburbs_and_Localities.geojson", help="Source suburb point GeoJSON")
    parser.add_argument("--aliases", type=Path, default=ROOT / "config/suburb-aliases.json", help="Explicit alias JSON mapping")
    parser.add_argument("--sheet", help="Read only this workbook sheet; otherwise read all sheets with the required header")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "public/data", help="Aggregate output directory")
    args = parser.parse_args(argv)
    try:
        geojson, report = prepare_members(args.workbook, args.suburbs, args.aliases, args.sheet)
        write_outputs(geojson, report, args.output_dir, (args.workbook, args.suburbs, args.aliases))
    except (OSError, ValueError, KeyError, TypeError, BadZipFile, ET.ParseError) as error:
        parser.exit(2, f"Membership preparation failed: {error}\n")
    print(json.dumps(report["totals"], separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
