"""Compatibility import for the domain package used by Blender and old scripts."""

from pathlib import Path
import sys

if (Path(__file__).parent / "swiss_gis").is_dir():
    from .swiss_gis.core import *
else:
    source = Path(__file__).resolve().parents[1] / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    from swiss_gis.core import *
