# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

# -*- coding: utf-8 -*-

from __future__ import print_function

import sys, re, os, subprocess, shutil, platform
from pathlib import Path

try:
    from setuptools import Extension, setup
    from setuptools.command.build_py import build_py as _build_py
    from setuptools.command.build_ext import build_ext
except ImportError:
    print(
        "The preferred way to invoke 'setup.py' is via pip, as in 'pip "
        "install .'. If you wish to run the setup script directly, you must "
        "first install the build dependencies listed in pyproject.toml!",
        file=sys.stderr,
    )
    raise

SOURCE_DIR = Path(__file__).parent.resolve()

if sys.platform.startswith("win"):
    PLATFORM = "windows"
elif sys.platform.startswith("linux"):
    PLATFORM = "linux"
elif sys.platform.startswith("darwin"):
    PLATFORM = "macos"
elif sys.platform.startswith("android"):
    PLATFORM = "android"
else:
    raise Exception(f"Unsupported platform: {sys.platform}")

# Detect architecture for platform-specific CMake presets
if PLATFORM == "windows":
    python_arch = platform.machine().lower()
    is_arm64 = python_arch in ("arm64", "aarch64")
    if is_arm64:
        CMAKE_PRESET = "windows-arm64-msvc"
        MSVC_PLAT_SPEC = "x86_arm64"
    else:
        CMAKE_PRESET = "windows-msvc"
        MSVC_PLAT_SPEC = "x64"
elif PLATFORM == "linux":
    CMAKE_PRESET = "linux-gcc"
    MSVC_PLAT_SPEC = None
elif PLATFORM == "macos":
    CMAKE_PRESET = "macos-arm64-clang"
    MSVC_PLAT_SPEC = None
elif PLATFORM == "android":
    android_abi = os.environ.get("ANDROID_ABI")
    if android_abi == "arm64-v8a":
        CMAKE_PRESET = "android-arm64"
    elif android_abi == "x86_64":
        CMAKE_PRESET = "android-x86_64"
    else:
        raise RuntimeError(
            "Unsupported ANDROID_ABI for slangpy build: "
            f"{android_abi} (expected 'arm64-v8a' or 'x86_64')"
        )
    MSVC_PLAT_SPEC = None
else:
    raise RuntimeError(f"Unsupported platform: {PLATFORM}")

CMAKE_CONFIG = "RelWithDebInfo"

# Check if native extension build is disabled
NO_CMAKE_BUILD = os.environ.get("NO_CMAKE_BUILD") == "1"

# Check if we're building a release wheel
BUILD_RELEASE_WHEEL = os.environ.get("BUILD_RELEASE_WHEEL") == "1"


# A CMakeExtension needs a sourcedir instead of a file list.
# The name must be the _single_ output extension from the CMake build.
# If you need multiple extensions, see scikit-build.
class CMakeExtension(Extension):
    def __init__(self, name: str, sourcedir: str = "") -> None:
        super().__init__(name, sources=[])
        self.sourcedir = os.fspath(Path(sourcedir).resolve())


class CMakeBuild(build_ext):
    def build_extension(self, ext: CMakeExtension) -> None:
        # Must be in this form due to bug in .resolve() only fixed in Python 3.10+
        ext_fullpath = Path.cwd() / self.get_ext_fullpath(ext.name)
        extdir = ext_fullpath.parent.resolve()

        # Setup environment variables
        env = os.environ.copy()
        if os.name == "nt":
            sys.path.append(str(Path(__file__).parent / "tools"))
            import msvc  # type: ignore

            env = msvc.msvc14_get_vc_env(MSVC_PLAT_SPEC)

        build_dir = str(SOURCE_DIR / "build/pip")

        # Wipe out the build directory if it exists
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)

        cmake_args = [
            "--preset",
            CMAKE_PRESET,
            "-B",
            build_dir,
            f"-DCMAKE_DEFAULT_BUILD_TYPE={CMAKE_CONFIG}",
            f"-DPython_FIND_REGISTRY:STRING=NEVER",
            f"-DCMAKE_INSTALL_PREFIX={extdir}",
            f"-DCMAKE_INSTALL_LIBDIR=.",
            f"-DCMAKE_INSTALL_BINDIR=.",
            f"-DCMAKE_INSTALL_INCLUDEDIR=include",
            f"-DCMAKE_INSTALL_DATAROOTDIR=.",
            "-DSGL_BUILD_EXAMPLES=OFF",
            "-DSGL_BUILD_TESTS=OFF",
        ]

        if PLATFORM == "android":
            toolchain_file = os.environ.get("CMAKE_TOOLCHAIN_FILE")
            assert toolchain_file, "CMAKE_TOOLCHAIN_FILE environment variable is not set!"

            python_prefix = (Path(toolchain_file).parent / "python" / "prefix").resolve()

            # Explicitly find headers and library to bypass FindPython flakiness on Android
            include_dirs = list(python_prefix.glob("include/python3.*"))
            cmake_args.append(f"-DPython_INCLUDE_DIR={include_dirs[0]}")

            # Check for shared library first, then static
            libs = list(python_prefix.glob("lib/libpython3.*.so"))
            cmake_args.append(f"-DPython_LIBRARY={libs[0]}")
        else:
            cmake_args.insert(5, f"-DPython_ROOT_DIR:PATH={sys.prefix}")

        if PLATFORM == "android":
            slang_root = (SOURCE_DIR.parent / "slang").resolve()
            android_abi = os.environ.get("ANDROID_ABI")

            cmake_args += [
                "-DSGL_LOCAL_SLANG=ON",
                f"-DSGL_LOCAL_SLANG_DIR={slang_root}",
                f"-DSGL_LOCAL_SLANG_BUILD_DIR=build-android-{android_abi}/{CMAKE_CONFIG}",
            ]

        is_wsl = os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
        if is_wsl:
            print("[setup.py] WSL detected, attempting path mapping for debug info...")
            try:
                wsl_src = str(SOURCE_DIR)
                win_src = subprocess.check_output(["wslpath", "-m", wsl_src]).decode("utf-8").strip() if shutil.which("wslpath") else wsl_src
                print(f"[setup.py] Path mapping: '{wsl_src}' -> '{win_src}'")
                if win_src != wsl_src:
                    flag = f"-fdebug-prefix-map={wsl_src}={win_src}"
                    cmake_args += [f"-DCMAKE_{x}_FLAGS={flag}" for x in ["C", "CXX"]]
                    print(f"[setup.py] Added build flag: {flag}")
            except Exception as e:
                print(f"[setup.py] Failed to setup WSL path mapping: {e}")

        if BUILD_RELEASE_WHEEL:
            cmake_args += [
                "-DSGL_PROJECT_DIR=",
                "-DSGL_SLANG_DEBUG_INFO=OFF",
            ]

        # Adding CMake arguments set as environment variable
        if "CMAKE_ARGS" in os.environ:
            cmake_args += [item for item in os.environ["CMAKE_ARGS"].split(" ") if item]

        # Configure, build and install
        subprocess.run(["cmake", *cmake_args], env=env, check=True)
        subprocess.run(
            ["cmake", "--build", build_dir, "--config", CMAKE_CONFIG], env=env, check=True
        )
        subprocess.run(
            ["cmake", "--install", build_dir, "--config", CMAKE_CONFIG], env=env, check=True
        )

        # Remove files that are not needed
        for file in ["slang-rhi.lib"]:
            path = extdir / file
            if path.exists():
                os.remove(path)


class CustomBuildPy(_build_py):
    def run(self):
        if BUILD_RELEASE_WHEEL:
            # Copy data/ into slangpy/data/ before building
            src = os.path.abspath("data")
            dst = os.path.join(self.build_lib, "slangpy", "data")
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

        # Continue normal build
        super().run()


VERSION_REGEX = re.compile(r"^\s*#\s*define\s+SGL_VERSION_([A-Z]+)\s+(.*)$", re.MULTILINE)

with open("src/sgl/sgl.h") as f:
    matches = dict(VERSION_REGEX.findall(f.read()))
    version = "{MAJOR}.{MINOR}.{PATCH}".format(**matches)
    print(f"version={version}")

with open("README.md", "r") as f:
    long_description = f.read()

setup(
    version=version,
    ext_modules=[] if NO_CMAKE_BUILD else [CMakeExtension("slangpy.slangpy_ext")],
    cmdclass={"build_ext": CMakeBuild, "build_py": CustomBuildPy},
    zip_safe=False,
)
