"""Build extension archives from clean staging directories."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ("scan_viewer", "swiss_gis", "railway_bim")
TARGETS = {"macos-arm64": "aarch64-apple-darwin"}
ARCHIVE_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def read_manifest(plugin: Path) -> dict:
    return tomllib.loads((plugin / "addon" / "blender_manifest.toml").read_text())


def host_platform() -> str:
    if platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        return "macos-arm64"
    raise RuntimeError("Release extensions currently support macOS arm64 only.")


def find_blender(value: str | None = None) -> str | None:
    configured = value or os.environ.get("BLENDER_BIN")
    if configured:
        return str(Path(configured).resolve()) if Path(configured).is_file() else None
    candidates = [shutil.which("blender")]
    if platform.system() == "Darwin":
        candidates.append("/Applications/Blender.app/Contents/MacOS/Blender")
    return next((str(Path(path).resolve()) for path in candidates if path and Path(path).is_file()), None)


def stage_plugin(plugin: Path, stage: Path) -> dict:
    """Copy only runtime code, manifests, licenses, and package data."""
    manifest = read_manifest(plugin)
    stage.mkdir(parents=True, exist_ok=False)
    shutil.copytree(plugin / "addon", stage, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "wheels", ".DS_Store"))
    shutil.copy2(plugin / "LICENSE", stage / "LICENSE")
    source = plugin / "src"
    if source.is_dir():
        for package in sorted(source.iterdir()):
            if package.is_dir() and (package / "__init__.py").is_file():
                shutil.copytree(package, stage / package.name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return manifest


def write_archive(stage: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if not path.is_file():
                    continue
                info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), ARCHIVE_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                mode = 0o755 if os.access(path, os.X_OK) else 0o644
                info.external_attr = (0o100000 | mode) << 16
                archive.writestr(info, path.read_bytes())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _expected_wheels(plugin: Path) -> dict[str, tuple[str, str]]:
    expected = {}
    for line in (plugin / "requirements-blender.lock").read_text().splitlines():
        line = line.partition("#")[0].strip()
        if line:
            requirement, digest = line.split()
            name, version = requirement.split("==")
            expected[name.replace("-", "_").lower()] = (version, digest.removeprefix("--hash=sha256:"))
    return expected


def _validate_wheels(plugin: Path, wheels: Path) -> None:
    expected = _expected_wheels(plugin)
    found = {}
    for path in wheels.glob("*.whl"):
        parts = path.name.split("-")
        if len(parts) < 5:
            raise RuntimeError(f"Invalid wheel filename: {path.name}")
        name, version = parts[:2]
        if parts[-1] != "any.whl":
            if parts[-3] != "cp313" or parts[-2] not in {"cp313", "abi3"} or not parts[-1].endswith("arm64.whl"):
                raise RuntimeError(f"Wheel does not support Blender's CPython 3.13/macOS arm64: {path.name}")
        if name in found:
            raise RuntimeError(f"Duplicate wheel for {name}")
        found[name] = (version, hashlib.sha256(path.read_bytes()).hexdigest())
    if found != expected:
        raise RuntimeError(f"Wheel inventory differs from the Blender lock: expected {expected}, got {found}")


def _stage_wheels(plugin: Path, stage: Path, reuse_wheels: bool) -> None:
    wheels = stage / "wheels"
    wheels.mkdir()
    if reuse_wheels:
        for path in (plugin / "addon" / "wheels").glob("*.whl"):
            shutil.copy2(path, wheels / path.name)
    else:
        subprocess.run([
            sys.executable, "-m", "pip", "download", "--no-deps", "--require-hashes", "--only-binary=:all:",
            "--python-version", "3.13", "--implementation", "cp", "--abi", "cp313",
            "--platform", "macosx_15_0_arm64", "--platform", "macosx_14_0_arm64",
            "--platform", "macosx_11_0_arm64", "--dest", str(wheels),
            "-r", str(plugin / "requirements-blender.lock"),
        ], check=True)
    _validate_wheels(plugin, wheels)
    path = stage / "blender_manifest.toml"
    manifest = path.read_text()
    declaration = "wheels = [\n" + "".join(
        f'    "./wheels/{wheel.name}",\n' for wheel in sorted(wheels.glob("*.whl"))) + "]\n"
    if "[permissions]" not in manifest or "\nwheels =" in manifest:
        raise RuntimeError("Expected an unbundled manifest with a permissions section.")
    path.write_text(manifest.replace("[permissions]", declaration + "\n[permissions]", 1))


def build_plugin(name: str, *, blender: str | None = None, reuse_wheels: bool = False) -> Path:
    if name not in PLUGINS:
        raise ValueError(f"Unknown plugin: {name}")
    plugin = ROOT / name
    manifest = read_manifest(plugin)
    target = host_platform()
    if target not in manifest.get("platforms", []):
        raise RuntimeError(f"{manifest['id']} does not declare support for {target}.")
    destination = plugin / "dist" / f"{manifest['id']}-{manifest['version']}-{target}.zip"
    with tempfile.TemporaryDirectory(prefix=f"{name}-release-") as folder:
        stage = Path(folder) / "source"
        stage_plugin(plugin, stage)
        if name == "scan_viewer":
            rust_target = TARGETS[target]
            subprocess.run(["cargo", "build", "--locked", "--release", "--target", rust_target],
                           cwd=plugin, check=True)
            binary = stage / "bin" / "scan-cache"
            binary.parent.mkdir(exist_ok=True)
            shutil.copy2(plugin / "target" / rust_target / "release" / "scan-cache", binary)
            binary.chmod(0o755)
        if name == "swiss_gis":
            _stage_wheels(plugin, stage, reuse_wheels)
        candidate = write_archive(stage, Path(folder) / destination.name)
        if blender:
            subprocess.run([blender, "--command", "extension", "validate", str(candidate)], check=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".partial",
                                                       dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(candidate, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination
