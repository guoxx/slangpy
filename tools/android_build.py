#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""
Android build helper for SlangPy wheels.

The cibuildwheel Android flow is split into three phases:
1. prepare: fetch/validate the Slang source tree and install host generators.
2. build_slang: cross-compile the ABI-specific Android Slang libraries.
3. repair_wheel: clean and patch the produced Android wheel.

Note on libc++_shared.so:
    We intentionally do NOT bundle libc++_shared.so in the wheel because:
    1. Android API 21+ provides /system/lib64/libc++.so
    2. The dynamic linker automatically falls back to the system library
    3. libc++.so and libc++_shared.so are ABI-compatible

    If you need to bundle libc++_shared.so for compatibility reasons, you can
    extend this script to copy it from the NDK and update NEEDED entries.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1]
SLANG_REPO_URL = "https://github.com/shader-slang/slang.git"
MANAGED_SLANG_ROOT = SOURCE_DIR / "build" / "android-slang-src"
CMAKE_CONFIG = "RelWithDebInfo"
ANDROID_PRESETS = {
    "arm64-v8a": "android-arm64",
    "x86_64": "android-x86_64",
}
CONFIG_PRESET_SUFFIXES = {
    "Debug": "debug",
    "Release": "release",
    "RelWithDebInfo": "releaseWithDebugInfo",
}
LOG_PREFIX = "[android_build.py]"


def _run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = False,
) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=capture_output,
        text=capture_output,
    )
    if capture_output:
        return result.stdout.strip()
    return ""


def _require_command(command: str) -> str:
    path = shutil.which(command)
    if not path:
        raise RuntimeError(f"Required command not found: {command}")
    return path


def _extract_slang_version() -> str:
    external_cmake = SOURCE_DIR / "external" / "CMakeLists.txt"
    match = re.search(
        r'set\(SGL_SLANG_VERSION\s+"([^"]+)"',
        external_cmake.read_text(encoding="utf-8"),
    )
    if not match:
        raise RuntimeError(f"Could not extract SGL_SLANG_VERSION from {external_cmake}")
    return match.group(1)


def _resolve_slang_root() -> Path:
    slang_dir = os.environ.get("SGL_LOCAL_SLANG_DIR", "").strip()
    if slang_dir:
        path = Path(slang_dir).expanduser()
        if not path.is_absolute():
            path = SOURCE_DIR / path
        return path.resolve()
    return MANAGED_SLANG_ROOT.resolve()


def _android_preset(android_abi: str) -> str:
    preset = ANDROID_PRESETS.get(android_abi)
    if preset is None:
        raise RuntimeError(f"Unsupported ANDROID_ABI for slang build: {android_abi}")
    return preset


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _require_git_lfs(cwd: Path) -> None:
    try:
        _run_command(["git", "lfs", "version"], cwd=cwd, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise RuntimeError(
            "git-lfs is required to fetch slang repository LFS files, "
            "but it is not available in the current build environment."
        )


def _managed_slang_matches_tag(slang_root: Path, expected_tag: str) -> bool:
    if not (slang_root / ".git").exists():
        return False

    try:
        tags_at_head = _run_command(
            ["git", "tag", "--points-at", "HEAD"], cwd=slang_root, capture_output=True
        )
    except subprocess.CalledProcessError:
        return False
    head_tags = [tag.strip() for tag in tags_at_head.splitlines() if tag.strip()]
    return expected_tag in head_tags


def _clone_managed_repo(slang_root: Path, slang_tag: str) -> None:
    slang_root.parent.mkdir(parents=True, exist_ok=True)
    _run_command(
        [
            "git",
            "clone",
            "--branch",
            slang_tag,
            "--single-branch",
            "--depth",
            "1",
            "--filter=blob:none",
            "--recurse-submodules",
            "--shallow-submodules",
            SLANG_REPO_URL,
            str(slang_root),
        ]
    )
    _run_command(["git", "lfs", "pull"], cwd=slang_root)
    _run_command(["git", "submodule", "foreach", "--recursive", "git lfs pull"], cwd=slang_root)


def _prepare_managed_slang_source(slang_root: Path, slang_tag: str) -> None:
    if slang_root.exists():
        if _managed_slang_matches_tag(slang_root, slang_tag):
            print(f"{LOG_PREFIX} Reusing managed slang source: {slang_root} ({slang_tag})")
            return
        print(
            f"{LOG_PREFIX} Managed slang source is not at {slang_tag}; removing and re-cloning."
        )
        _remove_path(slang_root)

    _require_git_lfs(SOURCE_DIR)
    print(f"{LOG_PREFIX} Cloning managed slang repository: {SLANG_REPO_URL} @ {slang_tag}")
    _clone_managed_repo(slang_root, slang_tag)
    if not _managed_slang_matches_tag(slang_root, slang_tag):
        raise RuntimeError(f"Downloaded slang repository is not at expected tag: {slang_tag}")


def _prepare_slang_source() -> Path:
    slang_root = _resolve_slang_root()
    if os.environ.get("SGL_LOCAL_SLANG_DIR", "").strip():
        if not slang_root.exists():
            raise RuntimeError(f"SGL_LOCAL_SLANG_DIR does not exist: {slang_root}")
        print(f"{LOG_PREFIX} Using user-provided slang root: {slang_root}")
        return slang_root

    slang_tag = f"v{_extract_slang_version()}"
    _prepare_managed_slang_source(slang_root, slang_tag)
    return slang_root


def prepare() -> None:
    slang_root = _prepare_slang_source()
    generators_prefix = slang_root / "build-platform-generators"

    # Follow Slang's documented Android flow: build host generators first, then
    # install them into a prefix consumed by the Android target build.
    print(f"{LOG_PREFIX} Building host slang generators in {slang_root}")
    _run_command(["cmake", "--workflow", "--preset", "generators"], cwd=slang_root)
    _run_command(
        [
            "cmake",
            "--install",
            "build",
            "--config",
            "Release",
            "--component",
            "generators",
            "--prefix",
            str(generators_prefix),
        ],
        cwd=slang_root,
    )
    print(f"{LOG_PREFIX} Host slang generators installed to {generators_prefix}")


def build_slang() -> None:
    slang_root = _resolve_slang_root()
    if not slang_root.exists():
        source_hint = (
            "SGL_LOCAL_SLANG_DIR"
            if os.environ.get("SGL_LOCAL_SLANG_DIR", "").strip()
            else "managed slang source"
        )
        raise RuntimeError(f"{source_hint} does not exist: {slang_root}. Run prepare first.")

    android_abi = os.environ.get("ANDROID_ABI", "").strip()
    if not android_abi:
        raise RuntimeError("ANDROID_ABI environment variable is not set")

    configure_preset = _android_preset(android_abi)
    build_dir = f"build-android-{android_abi}"
    build_root = slang_root / build_dir / CMAKE_CONFIG
    build_preset_suffix = CONFIG_PRESET_SUFFIXES.get(CMAKE_CONFIG)
    if build_preset_suffix is None:
        raise RuntimeError(f"Unsupported CMake config for slang build: {CMAKE_CONFIG}")

    generators_path = slang_root / "build-platform-generators" / "bin"
    if not generators_path.exists():
        raise RuntimeError(f"Slang generators are missing: {generators_path}. Run prepare first.")

    print(f"{LOG_PREFIX} Building Android slang target: {android_abi} in {build_root}")
    _run_command(
        [
            "cmake",
            "--preset",
            configure_preset,
            "--fresh",
            f"-DSLANG_GENERATORS_PATH={generators_path}",
            "-DSLANG_ENABLE_SPLIT_DEBUG_INFO=OFF",
        ],
        cwd=slang_root,
    )
    _run_command(
        ["cmake", "--build", "--preset", f"{configure_preset}-{build_preset_suffix}"],
        cwd=slang_root,
    )
    print(
        f"{LOG_PREFIX} Android slang build complete: "
        f"{slang_root} ({android_abi}, {CMAKE_CONFIG})"
    )


def _find_strip_command() -> str:
    """
    Find the appropriate strip command for Android cross-compilation.

    Search order:
    1. STRIP environment variable
    2. llvm-strip in ANDROID_NDK_HOME
    3. llvm-strip in ANDROID_HOME/ndk/*/
    """
    strip_cmd = os.environ.get("STRIP")
    if strip_cmd and shutil.which(strip_cmd):
        print(f"{LOG_PREFIX} Using STRIP from environment: {strip_cmd}")
        return strip_cmd

    ndk_paths: list[Path] = []
    if os.environ.get("ANDROID_NDK_HOME"):
        ndk_paths.append(Path(os.environ["ANDROID_NDK_HOME"]))

    if os.environ.get("ANDROID_HOME"):
        android_home = Path(os.environ["ANDROID_HOME"])
        ndk_dir = android_home / "ndk"
        if ndk_dir.exists():
            ndk_paths.extend(sorted(ndk_dir.iterdir(), reverse=True))

    for ndk_path in ndk_paths:
        if not ndk_path.exists():
            continue
        llvm_bins = list(ndk_path.glob("toolchains/llvm/prebuilt/*/bin/llvm-strip"))
        if llvm_bins:
            llvm_strip = llvm_bins[0]
            print(f"{LOG_PREFIX} Found llvm-strip in NDK: {llvm_strip}")
            return str(llvm_strip)

    android_home = os.environ.get("ANDROID_HOME", "<not set>")
    ndk_home = os.environ.get("ANDROID_NDK_HOME", "<not set>")
    raise RuntimeError(
        "Could not find llvm-strip in NDK. "
        f"ANDROID_HOME={android_home}, ANDROID_NDK_HOME={ndk_home}. "
        f"Searched paths: {[str(p) for p in ndk_paths]}. "
        "Please ensure NDK is properly installed. "
        "Set BUILD_RELEASE_WHEEL=0 to skip stripping."
    )


def _strip_debug_symbols(so_file: Path, strip_cmd: str) -> None:
    _run_command([strip_cmd, "--strip-debug", str(so_file)])
    print(f"{LOG_PREFIX} Stripped debug symbols from {so_file.name}")


def _set_runpath(so_file: Path, runpath: str, patchelf: str) -> None:
    _run_command([patchelf, "--set-rpath", runpath, str(so_file)])
    print(f"{LOG_PREFIX} Set RUNPATH={runpath} for {so_file.name}")


def repair_wheel(wheel_path: Path, dest_dir: Path) -> None:
    print(f"{LOG_PREFIX} Repairing wheel: {wheel_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        unpacked_dir = tmpdir_path / "unpacked"
        unpacked_dir.mkdir()

        print(f"{LOG_PREFIX} Extracting wheel to {unpacked_dir}")
        shutil.unpack_archive(wheel_path, unpacked_dir, format="zip")

        slangpy_dir = unpacked_dir / "slangpy"
        if not slangpy_dir.exists():
            raise RuntimeError(f"slangpy directory not found in wheel: {wheel_path}")

        static_libs = list(slangpy_dir.rglob("*.a"))
        if static_libs:
            print(f"{LOG_PREFIX} Found {len(static_libs)} static libraries (.a) - removing them")
            for lib in static_libs:
                print(f"{LOG_PREFIX} Removing {lib.name}")
                lib.unlink()

        so_files = sorted(slangpy_dir.glob("*.so"))
        if not so_files:
            raise RuntimeError(
                f"No shared libraries found in wheel package directory: {slangpy_dir}"
            )
        print(f"{LOG_PREFIX} Found {len(so_files)} .so files in slangpy/")

        build_release_wheel = os.environ.get("BUILD_RELEASE_WHEEL", "0") == "1"
        if build_release_wheel:
            print(f"{LOG_PREFIX} BUILD_RELEASE_WHEEL=1, stripping debug symbols")
            strip_cmd = _find_strip_command()
            for so_file in so_files:
                _strip_debug_symbols(so_file, strip_cmd)
        else:
            print(f"{LOG_PREFIX} BUILD_RELEASE_WHEEL not set, skipping strip")

        # Set RUNPATH=$ORIGIN for all .so files so they can find each other in
        # the same package directory.
        runpath = "$ORIGIN"
        print(f"{LOG_PREFIX} Setting RUNPATH={runpath} for all libraries")

        # Set RUNPATH after stripping. Some strip tools can rewrite ELF metadata
        # in ways that would drop or corrupt a prior patchelf update.
        patchelf = _require_command("patchelf")
        for so_file in so_files:
            _set_runpath(so_file, runpath, patchelf)

        dest_dir.mkdir(parents=True, exist_ok=True)
        print(f"{LOG_PREFIX} Repackaging wheel to {dest_dir}")
        _run_command(["wheel", "pack", str(unpacked_dir), "-d", str(dest_dir)])
        print(f"{LOG_PREFIX} Successfully repaired wheel: {dest_dir / wheel_path.name}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Android SlangPy build helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("prepare", help="Prepare Slang source and host generators")
    subparsers.add_parser("build_slang", help="Build Android Slang for ANDROID_ABI")

    repair_parser = subparsers.add_parser(
        "repair_wheel", help="Repair Android wheel shared libraries"
    )
    repair_parser.add_argument("wheel", type=Path, help="Path to the wheel file to repair")
    repair_parser.add_argument(
        "-w",
        "--dest-dir",
        type=Path,
        required=True,
        help="Destination directory for repaired wheel",
    )

    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "build_slang":
        build_slang()
    elif args.command == "repair_wheel":
        repair_wheel(args.wheel, args.dest_dir)
    else:
        raise RuntimeError(f"Unsupported command: {args.command}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(
            f"{LOG_PREFIX} ERROR: command failed: {exc.cmd} (exit {exc.returncode})",
            file=sys.stderr,
        )
        raise SystemExit(exc.returncode)
    except Exception as exc:
        print(f"{LOG_PREFIX} ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
