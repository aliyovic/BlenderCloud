"""Run the existing pilot acceptance suite with Blender's IfcOpenShell runtime.

The shared integration command supplies the reconstruction recorded by the
selected delivery. All export, edit, rejection, and corruption checks use
temporary copies through the existing unittest suite.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconstruction-dir", type=Path, required=True)
    arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = parser.parse_args(arguments)
    path = Path(__file__).with_name("test_ifc_delivery.py")
    spec = importlib.util.spec_from_file_location("railway_ifc_delivery_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.RECONSTRUCTION = args.reconstruction_dir.expanduser().resolve()
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if not suite.countTestCases():
        raise AssertionError("The pilot IFC acceptance suite contains no tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("The pilot IFC acceptance suite failed")


if __name__ == "__main__":
    main()
