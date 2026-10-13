"""Package Railway BIM without source scans or generated models."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from build_plugin import main
from build_support import build_plugin


def build_extension():
    return build_plugin("railway_bim")


if __name__ == "__main__":
    sys.argv.insert(1, "railway_bim")
    main()
