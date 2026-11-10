"""ArcGIS Pro entry point for local, distance-only OSM graph extraction.

No Network Analyst extension is needed. Clone the ArcGIS Python environment and
install osmium and pyproj there, or choose another Python executable with those
packages. Windows ArcGIS needs a Windows Python executable: the Linux workspace
.venv/bin/python cannot run directly on Windows. This wrapper does not translate
paths or launch WSL; run the extractor CLI inside WSL for that workflow.

The extractor requires Python 3.10+. Choose package builds for the selected
interpreter, rather than copying the WSL environment. In particular, pyproj 3.8
requires Python 3.12+; Python 3.11 can use pyproj 3.7.2. Both that pyproj release
and osmium 4.3.2 publish Windows x64 wheels. SciPy is not required for extraction.
Package requirements: https://pypi.org/project/pyproj/3.8.0/ and
https://pypi.org/project/pyproj/3.7.2/ and https://pypi.org/project/osmium/4.3.2/.
"""

from pathlib import Path
import subprocess
import sys

import arcpy


ROOT = Path(__file__).resolve().parent


class Toolbox:
    def __init__(self):
        self.label = "Support Heatmap Road Network"
        self.alias = "support_heatmap_network"
        self.tools = [ExtractRoadGraph]


class ExtractRoadGraph:
    def __init__(self):
        self.label = "Extract local OSM car distance graph"
        self.description = (
            "Export a directed road graph from a local OSM PBF with EPSG:8059 metre lengths. "
            "Select a Python 3.10+ executable for this operating system with compatible osmium "
            "and pyproj installed (pyproj 3.8 needs Python 3.12+). Conditional rules "
            "and unsupported restrictions are recorded in graph metadata."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        pbf = arcpy.Parameter(displayName="Local OSM PBF", name="pbf",
                              datatype="DEFile", parameterType="Required", direction="Input")
        pbf.filter.list = ["pbf"]
        pbf.value = str(ROOT / "south-australia-261108.osm.pbf")
        output = arcpy.Parameter(displayName="Output graph JSON", name="output_graph",
                                 datatype="DEFile", parameterType="Required", direction="Output")
        output.filter.list = ["json"]
        output.value = str(ROOT / ".generated" / "network.json")
        python = arcpy.Parameter(displayName="Python executable (osmium and pyproj installed)",
                                 name="python_executable", datatype="DEFile",
                                 parameterType="Required", direction="Input")
        python.value = sys.executable
        return [pbf, output, python]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        if parameters[0].valueAsText and parameters[1].valueAsText:
            if Path(parameters[0].valueAsText).resolve() == Path(parameters[1].valueAsText).resolve():
                parameters[1].setErrorMessage("Output must differ from the input OSM PBF.")

    def execute(self, parameters, messages):
        pbf, output, python = [parameter.valueAsText for parameter in parameters]
        script = ROOT / "scripts" / "extract_network.py"
        command = [python, str(script), "--pbf", pbf, "--output", output]
        arcpy.SetProgressor("default", "Extracting local OSM car distance graph...")
        arcpy.AddMessage(f"Python: {python}")
        try:
            result = subprocess.run(command, cwd=str(ROOT), capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", check=False)
            if result.stdout.strip():
                arcpy.AddMessage(result.stdout.strip())
            if result.returncode:
                arcpy.AddError(result.stderr.strip() or f"Extractor exited with code {result.returncode}.")
                raise arcpy.ExecuteError
            if result.stderr.strip():
                arcpy.AddWarning(result.stderr.strip())
            arcpy.SetParameterAsText(1, output)
        except OSError as exc:
            arcpy.AddError(f"Could not run the selected Python: {exc}. Use a Python executable "
                           "for this operating system with osmium and pyproj installed.")
            raise arcpy.ExecuteError from exc
        finally:
            arcpy.ResetProgressor()
