#!/usr/bin/env python3
"""Build Chippy's distro packages (deb, rpm, pacman) with nfpm.

    packaging/build.py stage STAGE_DIR
        Lay out the installed files under STAGE_DIR/usr: the app and its bundled Python
        libraries in /usr/lib/chippy, the launcher, desktop file, icons and extension.
        Needs Python 3 with pip and network access.

    packaging/build.py check-version [TAG]
        Check that the app, pyproject.toml, the metainfo and the release tag (vX.Y.Z) agree,
        and that the release has notes.

    packaging/build.py release-notes [VERSION]
        Print the GitHub release notes: packaging/release-notes.md filled in with the
        release's <description> from the metainfo file.

    packaging/build.py package STAGE_DIR TARGET OUT_DIR
        Pack STAGE_DIR into OUT_DIR for TARGET (debian, fedora, opensuse or arch), with that
        distro's dependency names. Needs nfpm on PATH. Prints the package path.

The version comes from src/chippy/__init__.py; CHIPPY_RELEASE sets the package release (1).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "io.github.eboye.Chippy"
EXTENSION = "chippy@eboye.github.io"
LIB = "usr/lib/chippy"

# Bundled libraries that PyPI only has as source; built into pure-Python wheels first.
SOURCE_ONLY = {"micloud", "android-backup"}
# Bundled libraries with compiled code: their wheels are abi3, good for any Python 3.
ABI3 = {"pycryptodome"}

# What each distro must provide: GTK 4, libadwaita 1.6+ (Adw.Spinner), PyGObject, and the
# compiled Python libraries left out of the bundle. (The map draws with GtkSnapshot, so
# PyGObject's cairo support isn't needed.)
DEPENDS = {
    "debian": [
        "python3 (>= 3.13)",
        "python3-gi",
        "gir1.2-gtk-4.0",
        "gir1.2-adw-1 (>= 1.6)",
        "python3-cryptography",
        "python3-yaml",
        "python3-zeroconf",
    ],
    "fedora": [
        "python3 >= 3.13",
        "python3-gobject",
        "gtk4",
        "libadwaita >= 1.6",
        "python3-cryptography",
        "python3-pyyaml",
        "python3-zeroconf",
    ],
    "opensuse": [
        "python3 >= 3.13",
        "python3-gobject",
        "python3-gobject-Gdk",
        "typelib-1_0-Gtk-4_0",
        "typelib-1_0-Adw-1",
        "libadwaita-1-0 >= 1.6",
        "python3-cryptography",
        "python3-PyYAML",
        "python3-zeroconf",
    ],
    "arch": [
        "python>=3.13",
        "python-gobject",
        "gtk4",
        "libadwaita>=1.6",
        "python-cryptography",
        "python-yaml",
        "python-zeroconf",
    ],
}
# The robot token lives in the Secret Service; GNOME Keyring is the usual provider.
RECOMMENDS = {
    "debian": ["gnome-keyring"],
    "fedora": ["gnome-keyring"],
    "opensuse": ["gnome-keyring"],
    "arch": ["gnome-keyring"],
}
PACKAGER = {"debian": "deb", "fedora": "rpm", "opensuse": "rpm", "arch": "archlinux"}
SUMMARY = "Control your Dreame robot vacuum from GNOME"
DESCRIPTION = (
    "Chippy shows your Dreame robot vacuum's map with its rooms, lets you clean chosen rooms, "
    "edit the map (zones, walls, room names and settings) and get notified when a run "
    "finishes. An optional GNOME Shell extension adds quick controls to the top bar."
)

LAUNCHER = """#!/usr/bin/python3
import sys

sys.path.insert(0, "/usr/lib/chippy")
from chippy.app import main

sys.exit(main())
"""


def version() -> str:
    text = (ROOT / "src/chippy/__init__.py").read_text()
    return re.search(r'^VERSION = "([^"]+)"', text, re.M).group(1)


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, check=True)


def pip(*args: str) -> None:
    run(sys.executable, "-m", "pip", "--disable-pip-version-check", *args)


def requirements() -> list[str]:
    lines = (ROOT / "packaging/vendor-requirements.txt").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _name(req: str) -> str:
    return req.split("==")[0].lower()


def vendor(target: Path) -> None:
    reqs = requirements()
    with tempfile.TemporaryDirectory() as wheels:
        source_only = [r for r in reqs if _name(r) in SOURCE_ONLY]
        pip("wheel", "--no-deps", "--no-binary", ":all:", "-w", wheels, *source_only)
        # Pure Python only: never a wheel built for this machine's Python.
        pure = [r for r in reqs if _name(r) not in ABI3]
        pip(
            "install", "--no-deps", "--no-compile", "--target", str(target),
            "--only-binary", ":all:", "--find-links", wheels,
            "--implementation", "py", "--python-version", "3.13", "--abi", "none",
            "--platform", "any", *pure,
        )  # fmt: skip
        abi3 = [r for r in reqs if _name(r) in ABI3]
        pip(
            "install", "--no-deps", "--no-compile", "--target", str(target),
            "--only-binary", ":all:", "--implementation", "cp", "--python-version", "3.13",
            "--abi", "abi3", "--platform", "manylinux2014_x86_64", *abi3,
        )  # fmt: skip
    shutil.rmtree(target / "bin", ignore_errors=True)  # miiocli and friends: not shipped
    for so in target.rglob("*.so"):
        if not so.name.endswith(".abi3.so"):
            raise SystemExit(f"bundled library has a Python-version-specific module: {so}")


def stage(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    lib = out / LIB
    vendor(lib)
    shutil.copytree(
        ROOT / "src/chippy", lib / "chippy", ignore=shutil.ignore_patterns("__pycache__")
    )
    launcher = out / "usr/bin/chippy"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)
    share = out / "usr/share"
    data = ROOT / "data"
    for src, dst in (
        (f"{APP_ID}.desktop", "applications"),
        (f"{APP_ID}.metainfo.xml", "metainfo"),
    ):
        (share / dst).mkdir(parents=True, exist_ok=True)
        shutil.copy(data / src, share / dst / src)
    service = share / "dbus-1/services" / f"{APP_ID}.service"
    service.parent.mkdir(parents=True)
    service.write_text((data / f"{APP_ID}.service").read_text().replace("/app/bin/", "/usr/bin/"))
    shutil.copytree(data / "icons", share / "icons")
    shutil.copytree(ROOT / "extension" / EXTENSION, share / "chippy/extension" / EXTENSION)
    (share / "licenses/chippy").mkdir(parents=True)
    shutil.copy(ROOT / "LICENSE", share / "licenses/chippy/LICENSE")
    for path in out.rglob("*"):  # predictable permissions whatever the umask
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or path == launcher else 0o644)
    print(f"staged Chippy {version()} in {out}")


def contents(stage_dir: Path) -> list[dict]:
    """Own Chippy's two folders whole, and only single files elsewhere: never /usr/share and
    other shared folders, which belong to the distro's filesystem package."""
    own = [stage_dir / LIB, stage_dir / "usr/share/chippy"]
    entries = [
        {"src": str(d), "dst": "/" + str(d.relative_to(stage_dir)), "type": "tree"} for d in own
    ]
    for path in sorted(stage_dir.rglob("*")):
        if path.is_file() and not any(path.is_relative_to(d) for d in own):
            entries.append({"src": str(path), "dst": "/" + str(path.relative_to(stage_dir))})
    return entries


def package(stage_dir: Path, target: str, out: Path) -> Path:
    if target not in DEPENDS:
        raise SystemExit(f"unknown target {target!r}; use one of {', '.join(DEPENDS)}")
    ver, release = version(), os.environ.get("CHIPPY_RELEASE", "1")
    out.mkdir(parents=True, exist_ok=True)
    names = {
        "debian": f"chippy_{ver}-{release}_amd64.deb",
        "fedora": f"chippy-{ver}-{release}.fedora.x86_64.rpm",
        "opensuse": f"chippy-{ver}-{release}.opensuse.x86_64.rpm",
        "arch": f"chippy-{ver}-{release}-x86_64.pkg.tar.zst",
    }
    config = {
        "name": "chippy",
        "arch": "amd64",
        "platform": "linux",
        "version": ver,
        "release": release,
        "section": "utils",
        "priority": "optional",
        "maintainer": "eboye <624357+eboye@users.noreply.github.com>",
        # pacman shows one line; deb and rpm show the first line as the summary.
        "description": SUMMARY if target == "arch" else f"{SUMMARY}\n{DESCRIPTION}",
        "vendor": "eboye",
        "homepage": "https://github.com/eboye/chippy-gtk",
        "license": "GPL-3.0-or-later",
        "depends": DEPENDS[target],
        "recommends": RECOMMENDS[target],
        "contents": contents(stage_dir.resolve()),
        "rpm": {"group": "Applications/System", "summary": SUMMARY},
        "archlinux": {"packager": "eboye <624357+eboye@users.noreply.github.com>"},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        json.dump(config, fh, indent=2)  # JSON is YAML, and needs no YAML library here
    try:
        dest = out / names[target]
        run("nfpm", "package", "--config", fh.name, "--packager", PACKAGER[target],
            "--target", str(dest))  # fmt: skip
    finally:
        os.unlink(fh.name)
    print(dest)
    return dest


def check_version(tag: str | None) -> int:
    """Every place that names the version must agree, and with the release tag (vX.Y.Z)."""
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    metainfo = re.search(
        r'<release version="([^"]+)"', (ROOT / f"data/{APP_ID}.metainfo.xml").read_text()
    )
    found = {
        "src/chippy/__init__.py": version(),
        "pyproject.toml": pyproject.group(1) if pyproject else None,
        "metainfo (newest release)": metainfo.group(1) if metainfo else None,
    }
    if tag is not None:
        found["tag"] = tag.removeprefix("v") if tag.startswith("v") else f"{tag} (no leading v)"
    for where, value in found.items():
        print(f"{where}: {value}")
    if len(set(found.values())) != 1:
        print("versions disagree", file=sys.stderr)
        return 1
    try:
        release_notes(version())
    except ValueError as err:
        print(err, file=sys.stderr)
        return 1
    return 0


def _inline(element: ET.Element) -> str:
    """AppStream inline markup (<em>, <code>) as Markdown, whitespace collapsed."""
    parts = [element.text or ""]
    for child in element:
        mark = {"em": "*", "code": "`"}.get(child.tag, "")
        parts.append(f"{mark}{_inline(child)}{mark}{child.tail or ''}")
    return " ".join("".join(parts).split())


def highlights(ver: str) -> str:
    """The <description> of release `ver` in the metainfo file, as Markdown."""
    root = ET.parse(ROOT / f"data/{APP_ID}.metainfo.xml").getroot()
    release = root.find(f"releases/release[@version='{ver}']")
    description = release.find("description") if release is not None else None
    if description is None or not len(description):
        raise ValueError(
            f'data/{APP_ID}.metainfo.xml needs a <release version="{ver}"> with a '
            "<description>: it becomes the release notes"
        )
    blocks = []
    for block in description:
        if block.tag == "p":
            blocks.append(_inline(block))
        elif block.tag in ("ul", "ol"):
            bullet = "-" if block.tag == "ul" else "1."
            blocks.append("\n".join(f"{bullet} {_inline(li)}" for li in block.iter("li")))
    return "\n\n".join(blocks)


def release_notes(ver: str) -> str:
    template = (ROOT / "packaging/release-notes.md").read_text()
    template = re.sub(r"\A<!--.*?-->\n", "", template, flags=re.S)  # the note for maintainers
    return Template(template).substitute(version=ver, highlights=highlights(ver))


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "stage":
        stage(Path(argv[1]))
    elif len(argv) == 4 and argv[0] == "package":
        package(Path(argv[1]), argv[2], Path(argv[3]))
    elif argv == ["version"]:
        print(version())
    elif len(argv) in (1, 2) and argv[0] == "check-version":
        return check_version(argv[1] if len(argv) == 2 else None)
    elif len(argv) in (1, 2) and argv[0] == "release-notes":
        try:
            print(release_notes(argv[1] if len(argv) == 2 else version()), end="")
        except ValueError as err:
            print(err, file=sys.stderr)
            return 1
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
