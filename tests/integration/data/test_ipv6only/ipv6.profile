# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

description: "LXD profile for Canonical Kubernetes with ipv6 networking"
devices:
  eth0:
    name: eth0
    nictype: bridged
    parent: ipv6-br0
    type: nic
