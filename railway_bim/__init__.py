"""Expose the source package when running tools from this checkout."""

from pathlib import Path

__path__.append(str(Path(__file__).parent / "src" / "railway_bim"))
