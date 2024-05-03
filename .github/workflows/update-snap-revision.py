#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


import os
import json
import logging
import sys
from pathlib import Path
from urllib.request import Request, urlopen
import yaml

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("update-snap-revision")

ROOT = Path(__file__).parent / ".." / ".."
INSTALLATION = ROOT / "charms/worker/k8s/templates/snap_installation.yaml"
LICENSE = Path(__file__).read_text().splitlines(keepends=True)[1:4]

def find_current_revision(arch: str) -> None | str:
    content = yaml.safe_load(INSTALLATION.read_text())
    if arch_spec := content.get(arch):
        for value in arch_spec:
            if value.get("name") == "k8s":
                rev = value.get("revision")
                log.info("Currently arch=%s revision=%s", arch, rev)
                return rev


def find_snapstore_revision(track: str, arch: str, risk: str) -> str:
    URL = f"https://api.snapcraft.io/v2/snaps/info/k8s?architecture={arch}&fields=revision"
    HEADER = {"Snap-Device-Series": 16}
    req = Request(URL, headers=HEADER)
    with urlopen(req) as response:
        snap_resp = json.loads(response.read())

    for mapping in snap_resp["channel-map"]:
        if (channel := mapping.get("channel")) and (
            channel.get("architecture") == arch
            and (channel.get("risk") == risk if risk else True)
            and track in channel.get("track")
        ):
            rev = mapping.get("revision")
            log.info("SnapStore arch=%s revision=%s track=%s%s", arch, rev, track, f" risk={risk}" if risk else "")
            return rev
    log.warning("SnapStore arch=%s revision=%s track=%s%s", arch, "N/A", track, f" risk={risk}" if risk else "")


def update_current_revision(arch: str, rev: str):
    content = yaml.safe_load(INSTALLATION.read_text())
    if arch_spec := content.get(arch):
        for value in arch_spec:
            if value.get("name") == "k8s":
                value["revision"] = rev
    log.info("Updating  arch=%s revision=%s", arch, rev)
    with INSTALLATION.open("w") as f:
        f.writelines(LICENSE)
        f.write(yaml.safe_dump(content))


def update_github_env(variable:str, value:str):
    if github_output := os.environ.get("GITHUB_OUTPUT", None):
        with Path(github_output).open(mode="a+") as f:
            f.write(f"{variable}={value}")

if __name__ == "__main__":
    arch, track, risk = sys.argv[1:]
    current_rev = find_current_revision(arch)
    snapstore_rev = find_snapstore_revision(track, arch, risk)
    if (
        snapstore_rev and
        current_rev and
        current_rev != snapstore_rev
    ):
        update_current_revision(arch, snapstore_rev)
        update_github_env("result", snapstore_rev)
    else:
        log.info("No change arch=%s current=%s snapstore=%s", arch, current_rev, snapstore_rev)        
