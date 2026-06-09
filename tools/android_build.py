#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""
Android build helper for SlangPy wheels.

The cibuildwheel Android flow is split into two phases:
1. prepare: fetch/validate the Slang source tree and install host generators.
2. build_slang: cross-compile the Android arm64-v8a Slang libraries.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1]
SLANG_REPO_URL = "https://github.com/shader-slang/slang.git"
MANAGED_SLANG_ROOT = SOURCE_DIR / "build" / "android-slang-src"
ANDROID_PRESET = "android-arm64"
ANDROID_BUILD_DIR = "build-android-arm64-v8a"
CONFIG_PRESET_SUFFIXES = {
    "Debug": "debug",
    "Release": "release",
    "RelWithDebInfo": "releaseWithDebugInfo",
}
LOG_PREFIX = "[android_build.py]"


def _cmake_config() -> str:
    # return "Release" if os.environ.get("BUILD_RELEASE_WHEEL") == "1" else "RelWithDebInfo"
    return "RelWithDebInfo"


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
        print(f"{LOG_PREFIX} Managed slang source is not at {slang_tag}; removing and re-cloning.")
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

    cmake_config = _cmake_config()
    build_root = slang_root / ANDROID_BUILD_DIR / cmake_config
    build_preset_suffix = CONFIG_PRESET_SUFFIXES.get(cmake_config)
    if build_preset_suffix is None:
        raise RuntimeError(f"Unsupported CMake config for slang build: {cmake_config}")

    generators_path = slang_root / "build-platform-generators" / "bin"
    if not generators_path.exists():
        raise RuntimeError(f"Slang generators are missing: {generators_path}. Run prepare first.")

    print(f"{LOG_PREFIX} Building Android slang target: arm64-v8a in {build_root}")
    _run_command(
        [
            "cmake",
            "--preset",
            ANDROID_PRESET,
            "--fresh",
            f"-DSLANG_GENERATORS_PATH={generators_path}",
            "-DSLANG_ENABLE_SPLIT_DEBUG_INFO=OFF",
        ],
        cwd=slang_root,
    )
    _run_command(
        ["cmake", "--build", "--preset", f"{ANDROID_PRESET}-{build_preset_suffix}"],
        cwd=slang_root,
    )
    print(
        f"{LOG_PREFIX} Android slang build complete: " f"{slang_root} (arm64-v8a, {cmake_config})"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Android SlangPy build helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("prepare", help="Prepare Slang source and host generators")
    subparsers.add_parser("build_slang", help="Build Android arm64-v8a Slang")

    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "build_slang":
        build_slang()
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
