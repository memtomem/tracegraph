"""Verify the built distributions before anything publishes them.

    python scripts/verify_artifacts.py dist 0.2.0

Checks, in order:
  1. the distribution directory holds exactly the two files the release uploads, under
     exactly the names it must upload them under. Everything in that directory is handed
     to the publisher and the filenames travel into later steps, so a surprising name is
     refused here rather than carried forward.
  2. every member of both archives is a plain file at a plain relative path -- no `..`,
     no absolute path, nothing that resolves somewhere other than where it reads -- and
     the sdist carries what the allowlist in pyproject.toml intends and nothing else.
  3. the recorded version and distribution name agree across the tag, the sdist PKG-INFO
     and the wheel METADATA.

The allowlist is read from pyproject.toml so that adding a module or a contract needs no
edit here. That makes it a drift detector, not an independent statement of intent -- so
there is a second, hardcoded deny list below that refuses the directories this project
must never ship, whatever the allowlist later says.

There is no "rebuild the wheel from the sdist" pass: `uv build` already builds the wheel
from the sdist it just made, and byte reproducibility across machines is not a release
requirement here.

Standard library only: this runs in the release job before anything is installed.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tarfile
import tomllib
import zipfile

# Hatchling ships these regardless of the sdist allowlist, so tolerate them by name.
# `.gitignore` is the repository's VCS ignore file; an exclude entry for it has no effect.
FORCED_SDIST_MEMBERS = ("PKG-INFO", ".gitignore")

# `uv build` writes this into the output directory. It is a build-tool artifact, never
# uploaded, so it is tolerated there by name and nothing else is.
BUILD_TOOL_FILES = (".gitignore",)

# Independent of the allowlist above, and deliberately so: if someone widens
# [tool.hatch.build.targets.sdist] the allowlist stops objecting, and this does not.
# Tests are what makes the package trustworthy but are not part of it; docs, CI
# configuration, review logs and the lockfile are repository material, not build input.
DENIED_SDIST_PREFIXES = (
    "tests/",
    "docs/",
    ".github/",
    "scripts/",
    "examples/",
    ".dev-trio/",
)
DENIED_SDIST_FILES = ("uv.lock",)

# Present in the sdist or the release is not buildable / not publishable.
REQUIRED_SDIST_MEMBERS = (
    "PKG-INFO",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "SECURITY.md",
    "src/tracegraph/__init__.py",
)


class VerifyFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    # Keep validation active under python -O too.
    if not condition:
        raise VerifyFailure(message)


def normalize(name: str) -> str:
    """PEP 503/427 file-name form of a distribution name (`agent-tracegraph` -> `agent_tracegraph`)."""
    return re.sub(r"[-_.]+", "_", name).lower()


def read_project(pyproject: Path) -> tuple[str, list[str]]:
    """Return (distribution name, sdist include patterns) from pyproject.toml."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    name = data.get("project", {}).get("name")
    require(isinstance(name, str) and name, "pyproject.toml has no [project] name")
    # Walked key by key rather than chained: a missing table is the configuration
    # failure this check exists to explain, and a bare KeyError would report it as the
    # single word 'include'.
    section: object = data
    for key in ("tool", "hatch", "build", "targets", "sdist", "include"):
        require(isinstance(section, dict) and key in section,
                "pyproject.toml has no [tool.hatch.build.targets.sdist] include list; "
                "without it hatchling ships everything git tracks")
        section = section[key]
    require(isinstance(section, list) and section,
            "the [tool.hatch.build.targets.sdist] include list is empty")
    return name, list(section)


def unsafe_path(name: str) -> str:
    """Why this archive member is not a plain relative path, or "" if it is.

    Checked before the allowlist and the deny list, because neither is meaningful on a
    path that does not mean what it says: `src/tracegraph/../../docs/private.txt` starts
    with an allowlisted prefix and does not start with a denied one, yet lands in `docs/`
    when extracted -- and one more `..` puts it outside the archive root entirely, which
    is how an extracted source distribution writes over files nobody offered it.
    """
    stripped = name.rstrip("/")
    if not stripped:
        return "an empty name"
    if stripped.startswith("/"):
        return "an absolute path"
    if "\\" in stripped:
        # No member here legitimately contains one, and a Windows extractor would read it
        # as a separator -- so `a\..\..\b` would traverse after passing a check that
        # only splits on "/".
        return "a backslash"
    parts = stripped.split("/")
    if ".." in parts:
        return "a '..' component"
    if "." in parts:
        return "a '.' component"
    if "" in parts:
        return "an empty path segment"
    return ""


def allows(patterns: list[str], member: str) -> bool:
    """Does an sdist member match the hatchling include allowlist?

    Entries are root-anchored (`/src/tracegraph/`): a trailing slash is a directory
    prefix, anything else is an exact root file.
    """
    for raw in patterns:
        pattern = raw.lstrip("/")
        if pattern.endswith("/"):
            if member.startswith(pattern):
                return True
        elif member == pattern:
            return True
    return False


def check_distribution_files(dist: Path, sdist_name: str, wheel_name: str) -> None:
    require(dist.is_dir(), f"not a directory: {dist}")
    expected = {sdist_name, wheel_name}
    # Presence first, so a version that does not match what was built reads as "missing
    # the file the tag asks for" rather than "the file that is there is a surprise".
    for name in sorted(expected):
        path = dist / name
        require(not path.is_symlink(), f"symlink in {dist}: {name}")
        require(path.is_file(), f"missing {path}")
    for entry in sorted(dist.iterdir()):
        # is_symlink() first: is_file() follows the link, so a symlink named like a
        # distribution would otherwise pass as a regular file.
        require(not entry.is_symlink(), f"symlink in {dist}: {entry.name}")
        require(entry.name in expected or entry.name in BUILD_TOOL_FILES,
                f"unexpected file in {dist}, which is what gets published: {entry.name}")
        require(entry.is_file(), f"not a regular file: {entry.name}")


def check_sdist(path: Path, root: str, patterns: list[str]) -> tuple[int, str]:
    """Validate sdist members. Returns (member count, PKG-INFO text)."""
    members: list[str] = []
    pkg_info = ""
    with tarfile.open(path, "r:gz") as archive:
        for info in archive:
            require(info.isreg() or info.isdir(),
                    f"sdist member is not a plain file or directory: {info.name}")
            unsafe = unsafe_path(info.name)
            require(not unsafe, f"sdist member carries {unsafe}: {info.name}")
            parts = info.name.split("/", 1)
            require(parts[0] == root,
                    f"sdist member outside the {root}/ root: {info.name}")
            if len(parts) == 1 or not parts[1]:
                continue
            member = parts[1]
            if info.isdir():
                continue
            require(member not in members, f"duplicate sdist member: {member}")
            members.append(member)
            require(not any(member.startswith(prefix) for prefix in DENIED_SDIST_PREFIXES)
                    and member not in DENIED_SDIST_FILES,
                    f"sdist carries repository material that must never ship: {member}")
            require(member in FORCED_SDIST_MEMBERS or allows(patterns, member),
                    f"sdist carries a path outside the pyproject allowlist: {member}")
            if member == "PKG-INFO":
                handle = archive.extractfile(info)
                require(handle is not None, "sdist PKG-INFO could not be read")
                pkg_info = handle.read().decode("utf-8", "replace")
    for required in REQUIRED_SDIST_MEMBERS:
        require(required in members, f"sdist is missing {required}")
    return len(members), pkg_info


def read_wheel_metadata(path: Path, dist_name: str, version: str) -> str:
    wanted = f"{normalize(dist_name)}-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for name in names:
            # Same reasoning as the sdist: this file is uploaded and then extracted.
            unsafe = unsafe_path(name)
            require(not unsafe, f"wheel member carries {unsafe}: {name}")
        require(wanted in names, f"wheel has no {wanted}")
        return archive.read(wanted).decode("utf-8", "replace")


def metadata_field(text: str, field: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
        if not line.strip():
            break  # headers end at the first blank line; the body is not metadata
    return ""


def verify(dist: Path, version: str, pyproject: Path) -> dict:
    dist_name, patterns = read_project(pyproject)
    stem = f"{normalize(dist_name)}-{version}"
    sdist_name = f"{stem}.tar.gz"
    wheel_name = f"{stem}-py3-none-any.whl"

    check_distribution_files(dist, sdist_name, wheel_name)
    count, pkg_info = check_sdist(dist / sdist_name, stem, patterns)
    metadata = read_wheel_metadata(dist / wheel_name, dist_name, version)

    for label, text in (("sdist PKG-INFO", pkg_info), ("wheel METADATA", metadata)):
        found_name = metadata_field(text, "Name")
        found_version = metadata_field(text, "Version")
        require(normalize(found_name) == normalize(dist_name),
                f"{label} Name is {found_name!r}, expected {dist_name!r}")
        require(found_version == version,
                f"{label} Version is {found_version!r}, expected {version!r}")

    return {
        "sdist": sdist_name,
        "wheel": wheel_name,
        "sdist_members": count,
        "version": version,
        "name": dist_name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dist", type=Path, help="directory holding the built distributions")
    parser.add_argument("version", help="the version being released, from the tag")
    parser.add_argument("--pyproject", type=Path,
                        default=Path(__file__).resolve().parents[1] / "pyproject.toml",
                        help="pyproject.toml to read the sdist allowlist from")
    args = parser.parse_args(argv)

    try:
        result = verify(args.dist, args.version, args.pyproject)
    except (VerifyFailure, OSError, KeyError, tarfile.TarError, zipfile.BadZipFile,
            tomllib.TOMLDecodeError) as exc:
        print(f"Artifact verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"sdist: {result['sdist']} ({result['sdist_members']} members, all allowlisted)")
    print(f"wheel: {result['wheel']}")
    print(f"{result['name']} {result['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
