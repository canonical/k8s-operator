#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


import argparse
import os
import json
import logging
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen
import yaml

logging.basicConfig(format="%(levelname)-8s: %(message)s", level=logging.INFO)
log = logging.getLogger("update-snap-revision")
TRACK = f"{t}-classic" if (t := os.getenv("TRACK")) else None
RISK = r if (r := os.getenv("RISK")) else "beta"
ROOT = Path(__file__).parent / ".." / ".."
INSTALLATION = ROOT / "charms/worker/k8s/templates/snap_installation.yaml"
LICENSE = Path(__file__).read_text().splitlines(keepends=True)[1:4]


def _multiline_log(logger, message, *args, **kwargs):
    NEWLINE_INDENT = "\n          * "
    logger(message.replace("\n", NEWLINE_INDENT), *args, **kwargs)


def find_current_revision(arch: str) -> None | str:
    content = yaml.safe_load(INSTALLATION.read_text())
    if arch_spec := content.get(arch):
        for value in arch_spec:
            if value.get("name") == "k8s":
                rev = value.get("revision")
                log.info("Currently arch='%s' revision='%s'", arch, rev)
                return rev


def find_snapstore_revision(arch: str, track: str, risk: str) -> str:
    URL = f"https://api.snapcraft.io/v2/snaps/info/k8s?architecture={arch}&fields=revision"
    HEADER = {"Snap-Device-Series": 16}
    req = Request(URL, headers=HEADER)
    with urlopen(req) as response:
        snap_resp = json.loads(response.read())

    for mapping in snap_resp["channel-map"]:
        if (channel := mapping.get("channel")) and (
            channel.get("architecture") == arch
            and (channel.get("risk") == risk if risk else True)
            and track.startswith(channel.get("track"))
        ):
            rev = mapping.get("revision")
            log.info(
                "SnapStore arch='%s' revision='%s' track='%s'%s",
                arch,
                rev,
                track,
                f" risk='{risk}'" if risk else "",
            )
            return rev
    _multiline_log(
        log.warning,
        "Failed to find a revision matching the arch/track/risk\n"
        "SnapStore arch='%s' track='%s'%s",
        arch,
        track,
        f" risk='{risk}'" if risk else "",
    )


def update_current_revision(arch: str, rev: str):
    content = yaml.safe_load(INSTALLATION.read_text())
    if arch_spec := content.get(arch):
        for value in arch_spec:
            if value.get("name") == "k8s":
                value["revision"] = rev
    log.info("Updating arch=%s revision=%s", arch, rev)
    with INSTALLATION.open("w") as f:
        f.writelines(LICENSE)
        f.write(yaml.safe_dump(content))


def update_github_output(variable: str, value: str):
    if github_output := os.environ.get("GITHUB_OUTPUT", None):
        with Path(github_output).open(mode="a+") as f:
            f.write(f"{variable}={value}\n")


def locate(arch: str, track: str, risk: str) -> None | str:
    log.info("Locating snap revision for arch='%s' track='%s' risk='%s'", arch, track, risk)
    current_rev = find_current_revision(arch)
    snapstore_rev = find_snapstore_revision(arch, track, risk)
    if snapstore_rev and current_rev and current_rev != snapstore_rev:
        update_current_revision(arch, snapstore_rev)
        update_github_output("revision", snapstore_rev)
        commit_sha(snapstore_rev)
    elif current_rev and current_rev == snapstore_rev:
        log.info("No change for arch=%s current=%s", arch, current_rev)
        commit_sha(current_rev)
    else:
        log.info("No change arch=%s current=%s snapstore=%s", arch, current_rev, snapstore_rev)
    return snapstore_rev


def commit_sha(revision: int|str) -> str:
    log.info("Locating build commit for revision='%s'", revision)
    snap_file = f"k8s_{revision}.snap"
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.check_call(
            ["snap", "download", "k8s", f"--revision={revision}", f"--target-directory={tmpdir}"]
        )
        subprocess.run(["unsquashfs", "-d", "snapdir", snap_file], cwd=tmpdir, check=True)
        bom_file = Path(tmpdir) / "snapdir" / "bom.json"
        bom_data = json.loads(bom_file.read_text())
        sha = bom_data["k8s"]["revision"]
        log.info("Commit ID for revision=%s sha=%s", revision, sha)
        update_github_output("commit_sha", sha)
    return sha


def get_arg_or_default(name: str, arg_value: str | None, default: str | None) -> str:
    if arg_value:
        return arg_value
    if default:
        return default
    parser.error(f"{name} must be provided either as an argument or through environment variable")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-command script")
    subparsers = parser.add_subparsers(dest="command", required=True)

    locate_parser = subparsers.add_parser("locate", help="Locate a snap revision")
    locate_parser.add_argument("arch", choices=["amd64", "arm64"], help="Architecture to update")
    locate_parser.add_argument("track", nargs="?", default=None, type=str)
    locate_parser.add_argument("risk", nargs="?", default=None, type=str)

    commit_parser = subparsers.add_parser("commit-sha", help="Find commit SHA for a snap revision")
    commit_parser.add_argument("revision", type=str)

    args = parser.parse_args()

    if args.command == "locate":
        track = get_arg_or_default("track", args.track, TRACK)
        risk = get_arg_or_default("risk", args.risk, RISK)
        locate(args.arch, track, risk)
    elif args.command == "commit-sha":
        commit_sha(args.revision)
