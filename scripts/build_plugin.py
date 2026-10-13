"""Build one supported Blender extension and validate it when Blender is available."""

import argparse
import os

from build_support import PLUGINS, build_plugin, find_blender


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin", choices=PLUGINS)
    parser.add_argument("--blender", help="Blender executable, also configurable through BLENDER_BIN")
    parser.add_argument("--reuse-wheels", action="store_true")
    parser.add_argument("--skip-blender-validation", action="store_true")
    args = parser.parse_args()
    blender = None if args.skip_blender_validation else find_blender(args.blender)
    configured = args.blender or os.environ.get("BLENDER_BIN")
    if configured and not blender and not args.skip_blender_validation:
        parser.error(f"Blender executable does not exist: {configured}")
    print(build_plugin(args.plugin, blender=blender, reuse_wheels=args.reuse_wheels))


if __name__ == "__main__":
    main()
