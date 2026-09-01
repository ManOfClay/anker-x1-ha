#!/usr/bin/env python3
"""Compute and apply this fork's version: ``<upstream version>.<fork revision>``.

This repository tracks afewyards/anker-x1-ha and carries its own commits on
top, so a plain SemVer bump cannot say which upstream release is inside. The
version is therefore upstream's three segments plus a fourth counting fork
releases against that base::

    upstream 0.5.2  ->  0.5.2.1  ->  0.5.2.2
    upstream 0.5.3  ->  0.5.3.1

Ordering holds under awesomeversion -- the comparator Home Assistant and HACS
use -- so HACS offers every fork release as an update and upstream's next
release supersedes the fork revisions of the previous one:

    0.5.2.1 > 0.5.2      True
    0.5.2.2 > 0.5.2.1    True
    0.5.3   > 0.5.2.1    True

``0.5.2+fork.N`` was measured against the same comparator first and rejected:
SemVer clause 11 ignores build metadata for precedence, so awesomeversion
reports `0.5.2+fork.2 > 0.5.2+fork.1` as False and HACS would never offer a
fork release as an update at all.

The upstream base is NOT derived from tags. Both remotes tag ``vX.Y.Z`` and
the same number has already meant two different trees (fork v0.5.2 and
upstream v0.5.2 are unrelated commits), so guessing from tag names is not
safe. It is recorded in pyproject.toml under ``[tool.anker_x1_fork]`` and
updated by hand as part of each upstream merge -- that merge is a manual act
anyway, and it is the moment when the base is actually known.

The fork revision, in contrast, comes from the git tags: only ``v<base>.<n>``
with a numeric fourth segment is considered, which cannot collide with the
three-segment tags of either release line. Tags are the record of what was
actually published, so the counter stays correct even if the manifest drifts.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
MANIFEST = ROOT / "custom_components" / "anker_x1" / "manifest.json"

# Matches the manifest's version line without reformatting the rest of the file.
VERSION_LINE = re.compile(r'("version":\s*")([^"]*)(")')


def upstream_base() -> str:
    """The upstream release this fork currently sits on top of."""
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    try:
        base = data["tool"]["anker_x1_fork"]["upstream_version"]
    except KeyError:
        sys.exit(
            "pyproject.toml is missing [tool.anker_x1_fork] upstream_version -- "
            "set it to the upstream release that was merged."
        )
    if not re.fullmatch(r"\d+\.\d+\.\d+", base):
        sys.exit(f"upstream_version must be X.Y.Z, got {base!r}")
    return base


def released_revisions(base: str) -> list[int]:
    """Fork revisions already tagged against ``base``, from the git tags."""
    out = subprocess.run(
        ["git", "tag", "--list", f"v{base}.*"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    revisions = []
    for tag in out.split():
        suffix = tag.removeprefix(f"v{base}.")
        # Reject anything but a bare integer: v0.5.2.1-rc1 or a stray
        # v0.5.2.1.2 must not be read as a revision number.
        if suffix.isdigit():
            revisions.append(int(suffix))
    return sorted(revisions)


def next_version() -> str:
    base = upstream_base()
    revisions = released_revisions(base)
    return f"{base}.{revisions[-1] + 1 if revisions else 1}"


def manifest_version() -> str:
    match = VERSION_LINE.search(MANIFEST.read_text())
    if match is None:
        sys.exit(f"no version field found in {MANIFEST}")
    return match.group(2)


def warn_if_stale(version: str) -> None:
    """Warn when the manifest is not behind the version we just computed.

    The revision comes from the local tags, so a clone whose tags are stale --
    `git fetch --no-tags`, a shallow checkout -- computes a number that was
    already published, and --apply would walk the manifest backwards. The
    manifest sitting at or above the computed version is the cheap tell.

    Only a warning, never fatal: `manifest == next` is also the legitimate
    idempotent path, where the version was applied in an earlier commit and the
    release job finds nothing to commit before tagging. The two cannot be told
    apart without the very tag list that is missing, and the release job is the
    authority anyway -- its checkout always carries the tags.
    """
    def parts(value: str) -> tuple[int, ...]:
        return tuple(int(p) for p in re.findall(r"\d+", value))

    if parts(manifest_version()) >= parts(version):
        print(
            f"warning: manifest is already at {manifest_version()}, not behind the "
            f"computed {version} -- if that is unexpected, the local tags are stale: "
            "run `git fetch --tags` and try again.",
            file=sys.stderr,
        )


def apply(version: str) -> bool:
    """Write ``version`` into the manifest. Returns True if the file changed."""
    text = MANIFEST.read_text()
    updated = VERSION_LINE.sub(lambda m: f"{m.group(1)}{version}{m.group(3)}", text, count=1)
    if updated == text:
        return False
    MANIFEST.write_text(updated)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--print-next",
        action="store_true",
        help="print the next fork version without touching any file",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="write the next fork version into the HA manifest",
    )
    group.add_argument(
        "--current",
        action="store_true",
        help="print the version currently in the HA manifest",
    )
    args = parser.parse_args()

    if args.current:
        print(manifest_version())
        return

    version = next_version()
    warn_if_stale(version)
    if args.apply:
        changed = apply(version)
        print(f"{'set' if changed else 'already at'} {version}", file=sys.stderr)
    print(version)


if __name__ == "__main__":
    main()
