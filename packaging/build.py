"""Build the double-clickable app and prove the bundle is not silently broken.

    uv sync --extra desktop --extra pack
    uv run python packaging/build.py

What it does beyond calling PyInstaller:

* **Runs ``--selftest`` against the frozen binary.** PyInstaller happily produces
  an app that starts and then fails only when a review begins, because the
  prompt files were never bundled. The selftest opens the app's own view of
  ``STATIC_DIR`` and ``PROMPT_PATH`` and fails the build if either is missing.
* **Reports the size and the output path.** ``--onedir`` means "a folder", and
  on macOS "a folder in .app clothing"; people are surprised by 60–90 MB, and
  the number is mostly the Python runtime, not this project.

Not handled here: code signing and notarization. An ad-hoc signature
(``codesign -s -``) is enough to run the app on the machine that built it; giving
it to someone else needs a Developer ID and ``notarytool``.
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WORK = ROOT / "build"
SPEC = ROOT / "packaging" / "CodeErratum.spec"


def bundle_root() -> Path:
    """Directory to sign and to measure."""
    if sys.platform == "darwin":
        return DIST / "CodeErratum.app"
    return DIST / "CodeErratum"


def bundle_exe() -> Path:
    """The binary inside it — a .app is a directory, not an executable."""
    if sys.platform == "darwin":
        return bundle_root() / "Contents" / "MacOS" / "CodeErratum"
    if sys.platform == "win32":
        return bundle_root() / "CodeErratum.exe"
    return bundle_root() / "CodeErratum"


def du(path: Path) -> str:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return f"{total / 1024 / 1024:.0f} MB"


def selftest_output(lines: int = 24) -> str:
    """Tail of desktop.log — the only place a windowed Windows build can print.

    With ``console=False`` there is no stdout in the frozen process, so the
    selftest result reaches the terminal (and the CI log) exclusively through the
    log file. Empty output here means the redirect itself failed.
    """
    log = Path.home() / ".codeerratum" / "desktop.log"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(desktop.log is missing — the frozen selftest printed nowhere)"
    return "\n".join(text.splitlines()[-lines:])


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the desktop app")
    parser.add_argument("--clean", action="store_true", help="Remove build/ and dist/ first")
    parser.add_argument("--skip-selftest", action="store_true", help="Build without verifying")
    args = parser.parse_args()

    if args.clean:
        for path in (DIST, WORK):
            shutil.rmtree(path, ignore_errors=True)
    elif sys.platform == "darwin":
        # PyInstaller removes its COLLECT output before writing it again, and on
        # macOS that directory is only an intermediate: the .app already holds a
        # copy. Clearing the previous .app here keeps the rebuild deterministic
        # (and keeps the removal out of PyInstaller's own "not empty" path).
        for path in (DIST / "CodeErratum", DIST / "CodeErratum.app"):
            shutil.rmtree(path, ignore_errors=True)

    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)])

    exe = bundle_exe()
    if not exe.exists():
        print(f"build finished but {exe} is missing", file=sys.stderr)
        return 1

    if not args.skip_selftest:
        run([str(exe), "--selftest"])
        if sys.platform == "win32":
            print("--- selftest (from desktop.log: a windowed exe has no stdout) ---")
            print(selftest_output())

    target = bundle_root()
    if sys.platform == "darwin":
        # A missing icon is legal (fresh clone without artwork) but a spec that
        # points at an .icns the bundle then lacks means the icon was silently
        # dropped — worth failing over, since nobody notices until the Dock.
        # PyInstaller 6 keeps the source file name, so the plist is the only
        # reliable way to know what it wrote (it used to be icon-windowed.icns).
        src_icon = ROOT / "packaging" / "icon" / "CodeErratum.icns"
        icon_name = ""
        plist = target / "Contents" / "Info.plist"
        if plist.is_file():
            with plist.open("rb") as fh:
                icon_name = plistlib.load(fh).get("CFBundleIconFile") or ""
        baked = target / "Contents" / "Resources" / icon_name if icon_name else None
        if src_icon.is_file() and not (baked and baked.is_file()):
            print(f"icon exists but the bundle has none (plist: {icon_name!r})", file=sys.stderr)
            return 1
        print(f"icon:  {icon_name or 'generic'}")
    elif sys.platform == "win32":
        # Explorer reads the icon out of the .exe resource, so there is nothing to
        # verify post-build; report what went in, since a missing .ico is silently
        # accepted by PyInstaller and shows up as the generic icon forever.
        ico = ROOT / "packaging" / "icon" / "CodeErratum.ico"
        note = ico.name if ico.is_file() else "none (packaging/icon/CodeErratum.ico missing)"
        print(f"icon:  {note}")

    if sys.platform == "darwin":
        # Ad-hoc signature: without one, the app is killed on launch on Apple
        # silicon once the bundle has been modified (which building it does).
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(target)],
            check=False,
            capture_output=True,
        )

    print(f"\nbuilt: {target}")
    print(f"size:  {du(target)}")
    if sys.platform == "darwin":
        print(f"open:  open {target}")
    elif sys.platform == "win32":
        # Onedir again: there is no installer to run, the folder *is* the app.
        print(f"ship:  zip {target.name}\\ and hand it over (unzip anywhere, run CodeErratum.exe)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
