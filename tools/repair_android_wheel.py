#!/usr/bin/env python3
"""
Android wheel repair script that sets RUNPATH for all shared libraries.

This script sets RUNPATH=$ORIGIN for all .so files in the slangpy/ directory,
allowing them to find each other at runtime.

Note on libc++_shared.so:
    We intentionally do NOT bundle libc++_shared.so in the wheel because:
    1. Android API 21+ provides /system/lib64/libc++.so
    2. The dynamic linker automatically falls back to the system library
    3. libc++.so and libc++_shared.so are ABI-compatible

    If you need to bundle libc++_shared.so for compatibility reasons, you can
    extend this script to copy it from the NDK and update NEEDED entries.
"""

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def find_strip_command() -> str:
    """
    Find the appropriate strip command for Android cross-compilation.

    Search order:
    1. STRIP environment variable
    2. llvm-strip in ANDROID_NDK_HOME
    3. llvm-strip in ANDROID_HOME/ndk/*/

    Returns:
        Path to the strip command

    Raises:
        RuntimeError: If strip command cannot be found
    """
    # First try STRIP environment variable
    strip_cmd = os.environ.get("STRIP")
    if strip_cmd and shutil.which(strip_cmd):
        print(f"[repair_android_wheel] Using STRIP from environment: {strip_cmd}")
        return strip_cmd

    # Try to find llvm-strip in NDK
    ndk_paths = []

    # Check ANDROID_NDK_HOME
    if os.environ.get("ANDROID_NDK_HOME"):
        ndk_paths.append(Path(os.environ["ANDROID_NDK_HOME"]))

    # Check ANDROID_HOME/ndk/{version}/
    if os.environ.get("ANDROID_HOME"):
        android_home = Path(os.environ["ANDROID_HOME"])
        ndk_dir = android_home / "ndk"
        if ndk_dir.exists():
            ndk_paths.extend(sorted(ndk_dir.iterdir(), reverse=True))  # Try newest first

    # Search for llvm-strip in NDK paths
    for ndk_path in ndk_paths:
        if not ndk_path.exists():
            continue
        # NDK structure: toolchains/llvm/prebuilt/{host}/bin/llvm-strip
        llvm_bins = list(ndk_path.glob("toolchains/llvm/prebuilt/*/bin/llvm-strip"))
        if llvm_bins:
            llvm_strip = llvm_bins[0]
            print(f"[repair_android_wheel] Found llvm-strip in NDK: {llvm_strip}")
            return str(llvm_strip)

    # If nothing works, raise an error
    android_home = os.environ.get("ANDROID_HOME", "<not set>")
    ndk_home = os.environ.get("ANDROID_NDK_HOME", "<not set>")
    raise RuntimeError(
        "Could not find llvm-strip in NDK. "
        f"ANDROID_HOME={android_home}, ANDROID_NDK_HOME={ndk_home}. "
        f"Searched paths: {[str(p) for p in ndk_paths]}. "
        "Please ensure NDK is properly installed. "
        "Set BUILD_RELEASE_WHEEL=0 to skip stripping."
    )


def strip_debug_symbols(so_file: Path) -> bool:
    """
    Strip debug symbols from a shared library.

    Args:
        so_file: Path to the .so file

    Returns:
        True if successful, False otherwise

    Raises:
        RuntimeError: If strip command cannot be found
    """
    strip_cmd = find_strip_command()

    try:
        subprocess.run(
            [strip_cmd, "--strip-debug", str(so_file)],
            check=True,
            capture_output=True,
            text=True
        )
        print(f"[repair_android_wheel] Stripped debug symbols from {so_file.name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[repair_android_wheel] Warning: Failed to strip {so_file.name}")
        print(f"  Command: {strip_cmd}")
        print(f"  Error: {e.stderr}")
        return False
    except FileNotFoundError:
        print(f"[repair_android_wheel] Warning: strip command not found: {strip_cmd}")
        return False


def set_runpath(so_file: Path, runpath: str) -> bool:
    """
    Set RUNPATH for a shared library using patchelf.

    Args:
        so_file: Path to the .so file
        runpath: RUNPATH value to set (e.g., "$ORIGIN")

    Returns:
        True if successful, False otherwise
    """
    try:
        subprocess.run(
            ["patchelf", "--set-rpath", runpath, str(so_file)],
            check=True,
            capture_output=True,
            text=True
        )
        print(f"[repair_android_wheel] Set RUNPATH={runpath} for {so_file.name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[repair_android_wheel] Warning: Failed to set RUNPATH for {so_file.name}")
        print(f"  Error: {e.stderr}")
        return False
    except FileNotFoundError:
        print(f"[repair_android_wheel] Error: patchelf not found")
        print(f"  Install with: pip install patchelf")
        return False


def repair_wheel(wheel_path: Path, dest_dir: Path) -> None:
    """
    Repair Android wheel by setting RUNPATH for all shared libraries.

    Args:
        wheel_path: Path to the input wheel file
        dest_dir: Directory where the repaired wheel should be placed
    """
    print(f"[repair_android_wheel] Repairing wheel: {wheel_path}")

    # Debug: Print relevant environment variables
    # print(f"[repair_android_wheel] Environment variables:")
    # for key in ["STRIP", "BUILD_RELEASE_WHEEL", "ANDROID_NDK_HOME", "ANDROID_HOME", "ANDROID_ABI"]:
    #     value = os.environ.get(key, "<not set>")
    #     print(f"[repair_android_wheel]   {key}={value}")

    # Parse wheel name
    match = re.search(r"^(.+?)-", wheel_path.name)
    if not match:
        raise RuntimeError(f"Failed to parse wheel filename: {wheel_path.name}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        unpacked_dir = tmpdir_path / "unpacked"
        unpacked_dir.mkdir()

        # Extract wheel
        print(f"[repair_android_wheel] Extracting wheel to {unpacked_dir}")
        shutil.unpack_archive(wheel_path, unpacked_dir, format="zip")

        # Find slangpy package directory
        slangpy_dir = unpacked_dir / "slangpy"
        if not slangpy_dir.exists():
            raise RuntimeError(f"slangpy directory not found in wheel: {wheel_path}")

        # Remove .a files (static libraries)
        static_libs = list(slangpy_dir.rglob("*.a"))
        if static_libs:
            print(f"[repair_android_wheel] Found {len(static_libs)} static libraries (.a) - removing them")
            for lib in static_libs:
                print(f"[repair_android_wheel] Removing {lib.name}")
                lib.unlink()

        # Find all .so files in slangpy/ directory
        so_files = list(slangpy_dir.glob("*.so"))
        print(f"[repair_android_wheel] Found {len(so_files)} .so files in slangpy/")

        # Check if we should strip debug symbols (only for release wheels)
        build_release_wheel = os.environ.get("BUILD_RELEASE_WHEEL", "0") == "1"

        if build_release_wheel:
            print(f"[repair_android_wheel] BUILD_RELEASE_WHEEL=1, stripping debug symbols from all libraries")
            strip_count = 0
            for so_file in so_files:
                if strip_debug_symbols(so_file):
                    strip_count += 1
            print(f"[repair_android_wheel] Successfully stripped {strip_count}/{len(so_files)} libraries")
        else:
            print(f"[repair_android_wheel] BUILD_RELEASE_WHEEL not set, skipping strip")

        # Set RUNPATH=$ORIGIN for all .so files
        # This allows them to find each other in the same directory
        runpath = "$ORIGIN"
        print(f"[repair_android_wheel] Setting RUNPATH={runpath} for all libraries")

        # HACK: set RUNPATH must be placed after stripping to avoid corrupting the binary
        success_count = 0
        for so_file in so_files:
            if set_runpath(so_file, runpath):
                success_count += 1

        print(f"[repair_android_wheel] Successfully set RUNPATH for {success_count}/{len(so_files)} libraries")

        # Repackage wheel
        dest_dir.mkdir(parents=True, exist_ok=True)
        print(f"[repair_android_wheel] Repackaging wheel to {dest_dir}")
        subprocess.run(
            ["wheel", "pack", str(unpacked_dir), "-d", str(dest_dir)],
            check=True
        )

        print(f"[repair_android_wheel] Successfully repaired wheel: {dest_dir / wheel_path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair Android wheel by setting RUNPATH for all shared libraries"
    )
    parser.add_argument("wheel", type=Path, help="Path to the wheel file to repair")
    parser.add_argument("-w", "--dest-dir", type=Path, required=True,
                        help="Destination directory for repaired wheel")

    args = parser.parse_args()
    repair_wheel(args.wheel, args.dest_dir)


if __name__ == "__main__":
    main()

