"""Membership preparation tests; fixtures and XLSX generation use only stdlib."""

from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/prepare_members.py"
SPEC = importlib.util.spec_from_file_location("prepare_members", SCRIPT)
members = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(members)


def cell(reference, value):
    if value is None:
        return ""
    return f'<c r={quoteattr(reference)} t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def worksheet(rows):
    content = "".join(f'<row r="{number}">{cells}</row>' for number, cells in rows)
    return f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{content}</sheetData></worksheet>'


def workbook(path, sheets, shared_strings=None, absolute_targets=False):
    namespaces = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    sheet_nodes = []
    relationships = []
    with ZipFile(path, "w") as archive:
        for number, (name, contents) in enumerate(sheets.items(), 1):
            sheet_nodes.append(f'<sheet name={quoteattr(name)} sheetId="{number}" r:id="rId{number}"/>')
            target = f'worksheets/sheet{number}.xml'
            if absolute_targets:
                target = '/xl/' + target
            relationships.append(f'<Relationship Id="rId{number}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="{target}"/>')
            archive.writestr(f'xl/worksheets/sheet{number}.xml', contents)
        if shared_strings is not None:
            archive.writestr('xl/sharedStrings.xml', f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared_strings}</sst>')
            relationships.append('<Relationship Id="strings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>')
        archive.writestr('xl/workbook.xml', f'<workbook {namespaces}><sheets>{"".join(sheet_nodes)}</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(relationships) + '</Relationships>')


def suburb(object_id, name, postcode="5000"):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [138.6, -34.9]},
        "properties": {"OBJECTID": object_id, "suburb": name, "postcode": postcode, "privateSourceField": "must not be exported"},
    }


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source.xlsx"
        self.suburbs = self.directory / "suburbs.geojson"
        self.aliases = self.directory / "aliases.json"

    def inputs(self, values, features=None, aliases=None, header="Residential Suburb"):
        rows = [(1, cell("A1", header))]
        rows.extend((number, cell(f"A{number}", value)) for number, value in enumerate(values, 2))
        workbook(self.source, {"Members": worksheet(rows)})
        self.suburbs.write_text(json.dumps({"type": "FeatureCollection", "features": features if features is not None else [suburb(1, "ALPHA"), suburb(2, "BETA", "0872")]}), encoding="utf-8")
        self.aliases.write_text(json.dumps(aliases if aliases is not None else {}), encoding="utf-8")

    def prepare(self):
        return members.prepare_members(self.source, self.suburbs, self.aliases)

    def assert_reconciles(self, geojson, report):
        totals = report["totals"]
        self.assertEqual(totals["sourceRows"], totals["memberRecords"] + totals["blankRows"])
        self.assertEqual(totals["memberRecords"], totals["joinedMembers"] + totals["unresolvedMembers"])
        self.assertEqual(totals["unresolvedMembers"], totals["unmatchedMembers"] + totals["ambiguousMembers"])
        self.assertEqual(totals["unresolvedMembers"], sum(item["count"] for item in report["unresolved"]))
        self.assertEqual(totals["joinedMembers"], sum(feature["properties"]["members"] for feature in geojson["features"]))

    def test_unicode_whitespace_case_and_repeated_rows(self):
        self.inputs(["  North   Adelaide ", "ＮＯＲＴＨ　ＡＤＥＬＡＩＤＥ", "north\tadelaide", "café", "cafe\u0301", None, "\u00a0\t"], [suburb(10, "NORTH ADELAIDE"), suburb(20, "CAFÉ"), suburb(30, "EMPTY")], header="  ｒｅｓｉｄｅｎｔｉａｌ\u00a0Ｓｕｂｕｒｂ ")
        geojson, report = self.prepare()
        self.assertEqual([f["properties"]["members"] for f in geojson["features"]], [3, 2, 0])
        self.assertEqual([f["properties"]["membershipMatch"] for f in geojson["features"]], ["exact", "exact", "none"])
        self.assertEqual(report["totals"]["sourceRows"], 7)
        self.assertEqual(report["totals"]["blankRows"], 2)
        self.assert_reconciles(geojson, report)

    def test_explicit_aliases_and_mixed_match_provenance(self):
        self.inputs(["Alpha", " A   Town ", "a town", "Beta Place", "Beta Place"], aliases={"A Town": "ALPHA", "Beta Place": "suburb-2"})
        geojson, report = self.prepare()
        self.assertEqual([f["properties"]["members"] for f in geojson["features"]], [3, 2])
        self.assertEqual([f["properties"]["membershipMatch"] for f in geojson["features"]], ["mixed", "alias"])
        self.assertEqual(report["unresolved"], [])
        self.assert_reconciles(geojson, report)

    def test_unmatched_names_aggregate_without_guessing_or_deduplicating(self):
        self.inputs(["Unknown", " UNKNOWN ", "Alpah", "UNKNOWN", "Alpha", None])
        geojson, report = self.prepare()
        self.assertEqual(report["unresolved"], [{"name": "alpah", "count": 1, "reason": "unmatched"}, {"name": "unknown", "count": 3, "reason": "unmatched"}])
        self.assertEqual(report["totals"]["memberRecords"], 5)
        self.assertEqual(report["totals"]["blankRows"], 1)
        self.assert_reconciles(geojson, report)

    def test_duplicate_suburb_names_remain_ambiguous_with_candidates(self):
        self.inputs(["Twin", "TWIN", "Mystery"], [suburb(9, "TWIN", "5001"), suburb(3, "TWIN", "5002")])
        geojson, report = self.prepare()
        ambiguous = next(item for item in report["unresolved"] if item["reason"] == "ambiguous")
        self.assertEqual(ambiguous["count"], 2)
        self.assertEqual(ambiguous["candidates"], [{"id": "suburb-3", "name": "TWIN", "postcode": "5002"}, {"id": "suburb-9", "name": "TWIN", "postcode": "5001"}])
        self.assertEqual(report["totals"]["ambiguousMembers"], 2)
        self.assertEqual(report["totals"]["unmatchedMembers"], 1)
        self.assert_reconciles(geojson, report)

    def test_alias_to_duplicate_name_does_not_silently_pick_candidate(self):
        self.inputs(["Twin Post"], [suburb(1, "TWIN"), suburb(2, "TWIN")], {"Twin Post": "Twin"})
        geojson, report = self.prepare()
        self.assertEqual(report["totals"]["ambiguousMembers"], 1)
        self.assertEqual(report["totals"]["joinedMembers"], 0)
        self.assert_reconciles(geojson, report)

    def test_explicit_stable_id_can_resolve_ambiguous_name(self):
        self.inputs(["Twin", "Twin"], [suburb(1, "TWIN"), suburb(2, "TWIN")], {"Twin": "suburb-2"})
        geojson, report = self.prepare()
        self.assertEqual([f["properties"]["members"] for f in geojson["features"]], [0, 2])
        self.assert_reconciles(geojson, report)

    def test_alias_configuration_errors_are_not_silent(self):
        for aliases, message in [({"A Town": "Nowhere"}, "absent"), ({"A Town": "Alpha", " a  town ": "Beta"}, "Conflicting"), ({"A Town": ""}, "nonblank"), ({"A Town": ["Alpha"]}, "nonblank"), ([], "JSON object")]:
            with self.subTest(aliases=aliases):
                self.inputs(["Alpha"], aliases=aliases)
                with self.assertRaisesRegex(ValueError, message):
                    self.prepare()

    def test_stable_ids_leading_zero_postcodes_and_property_whitelist(self):
        self.inputs(["Alpha"], [suburb(20, "BETA", "0872"), suburb(2, "ALPHA", 800)])
        geojson, report = self.prepare()
        self.assertEqual([f["id"] for f in geojson["features"]], ["suburb-2", "suburb-20"])
        self.assertEqual([f["properties"]["postcode"] for f in geojson["features"]], ["0800", "0872"])
        for feature in geojson["features"]:
            self.assertEqual(feature["id"], feature["properties"]["id"])
            self.assertEqual(set(feature["properties"]), {"id", "name", "postcode", "members", "membershipMatch"})
        self.assertEqual(set(report), {"totals", "unresolved"})

    def test_duplicate_ids_and_invalid_geometry_fail(self):
        invalid = suburb(2, "BETA")
        invalid["geometry"]["coordinates"] = [200, -34]
        for features in [[suburb(1, "ALPHA"), suburb(1, "BETA")], [invalid]]:
            with self.subTest(features=features):
                self.inputs(["Alpha"], features)
                with self.assertRaises(ValueError):
                    self.prepare()

    def test_shared_strings_rich_text_and_absolute_relationships(self):
        shared = '<si><t>Residential Suburb</t></si><si><r><t>AL</t></r><r><t>PHA</t></r><rPh sb="0" eb="2"><t>phonetic</t></rPh></si>'
        rows = [(1, '<c r="AA1" t="s"><v>0</v></c>'), (2, '<c r="AA2" t="s"><v>1</v></c>'), (3, '<c r="AA3" t="inlineStr"><is><r><t>AL</t></r><r><t>PHA</t></r></is></c>')]
        workbook(self.source, {"Members": worksheet(rows)}, shared, absolute_targets=True)
        self.assertEqual(members.workbook_counts(self.source), (Counter({"alpha": 2}), 2, 0))

    def test_sparse_rows_blank_suburb_and_leading_blank_rows(self):
        rows = [(1, ""), (3, cell("B3", "Residential Suburb")), (4, cell("B4", "Alpha")), (7, cell("A7", "other private column")), (8, "")]
        workbook(self.source, {"Members": worksheet(rows)})
        self.assertEqual(members.workbook_counts(self.source), (Counter({"alpha": 1}), 5, 4))

    def test_all_member_sheets_and_explicit_sheet_selection(self):
        data = worksheet([(1, cell("A1", "Residential Suburb")), (2, cell("A2", "Alpha"))])
        workbook(self.source, {"Notes": worksheet([(1, cell("A1", "Notes"))]), "One": data, "Two": data})
        self.assertEqual(members.workbook_counts(self.source), (Counter({"alpha": 2}), 2, 0))
        self.assertEqual(members.workbook_counts(self.source, "One"), (Counter({"alpha": 1}), 1, 0))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            members.workbook_counts(self.source, "Missing")

    def test_missing_and_duplicate_headers_fail(self):
        for cells in [cell("A1", "Wrong column"), cell("A1", "Residential Suburb") + cell("B1", " residential suburb ")]:
            with self.subTest(cells=cells):
                workbook(self.source, {"Members": worksheet([(1, cells)])})
                with self.assertRaises(ValueError):
                    members.workbook_counts(self.source)

    def test_formula_with_cached_value_counts_and_without_cache_fails(self):
        header = (1, cell("A1", "Residential Suburb"))
        workbook(self.source, {"Members": worksheet([header, (2, '<c r="A2" t="str"><f>"Alpha"</f><v>Alpha</v></c>')])})
        self.assertEqual(members.workbook_counts(self.source), (Counter({"alpha": 1}), 1, 0))
        workbook(self.source, {"Members": worksheet([header, (2, '<c r="A2"><f>"Alpha"</f><v/></c>')])})
        with self.assertRaisesRegex(ValueError, "no cached value"):
            members.workbook_counts(self.source)

    def test_empty_workbook_data_reconciles(self):
        self.inputs([])
        geojson, report = self.prepare()
        self.assertTrue(all(value == 0 for value in report["totals"].values()))
        self.assertEqual(len(geojson["features"]), 2)
        self.assert_reconciles(geojson, report)

    def test_cli_outputs_are_aggregate_deterministic_and_preserve_sources(self):
        self.inputs(["Alpha", "Alpha", "Unknown", "Unknown", None])
        sources = (self.source, self.suburbs, self.aliases)
        before = [hashlib.sha256(path.read_bytes()).digest() for path in sources]
        output = self.directory / "public/data"
        command = [sys.executable, "-B", str(SCRIPT), "--source", str(self.source), "--suburbs", str(self.suburbs), "--aliases", str(self.aliases), "--output-dir", str(output)]
        first = subprocess.run(command, cwd=self.directory, capture_output=True, text=True, check=True)
        files = sorted(output.iterdir())
        self.assertEqual([p.name for p in files], ["members.geojson", "membership-report.json"])
        first_bytes = [p.read_bytes() for p in files]
        subprocess.run(command, cwd=self.directory, capture_output=True, text=True, check=True)
        self.assertEqual(first_bytes, [p.read_bytes() for p in files])
        self.assertEqual(before, [hashlib.sha256(path.read_bytes()).digest() for path in sources])
        self.assertEqual(json.loads(first.stdout)["memberRecords"], 4)
        geojson = json.loads((output / "members.geojson").read_text())
        report = json.loads((output / "membership-report.json").read_text())
        self.assertEqual(report["unresolved"], [{"name": "unknown", "count": 2, "reason": "unmatched"}])
        self.assert_reconciles(geojson, report)

    def test_output_cannot_overwrite_source(self):
        self.inputs(["Alpha"])
        geojson, report = self.prepare()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            members.write_outputs(geojson, report, self.directory, (self.directory / "members.geojson",))


class ActualSourceTests(unittest.TestCase):
    def test_october_2026_source_and_alias_reconciliation(self):
        source = ROOT / "Heat Map Data October 2026.xlsx"
        suburbs = ROOT / "Suburbs_and_Localities.geojson"
        aliases = ROOT / "config/suburb-aliases.json"
        if not all(path.exists() for path in (source, suburbs, aliases)):
            self.skipTest("Original source dataset is not present")
        geojson, report = members.prepare_members(source, suburbs, aliases)
        self.assertEqual(report["totals"], {"sourceRows": 3105, "memberRecords": 3105, "joinedMembers": 3019, "unresolvedMembers": 86, "blankRows": 0, "unmatchedMembers": 86, "ambiguousMembers": 0})
        self.assertEqual(len(geojson["features"]), 1895)
        self.assertEqual(len(report["unresolved"]), 38)
        counts, source_rows, blank_rows = members.workbook_counts(source)
        _, by_name, _ = members.suburb_features(json.loads(suburbs.read_text()))
        direct = sum(count for name, count in counts.items() if len(by_name.get(name, [])) == 1)
        self.assertEqual((direct, source_rows, blank_rows), (3014, 3105, 0))
        self.assertEqual(report["totals"]["joinedMembers"] - direct, 5)


if __name__ == "__main__":
    unittest.main()
