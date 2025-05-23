# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
description: "LXD profile for Canonical Kubernetes"
config:
  boot.autostart: "true"
  linux.kernel_modules: ip_vs,ip_vs_rr,ip_vs_wrr,ip_vs_sh,ip_tables,ip6_tables,iptable_raw,netlink_diag,nf_nat,overlay,br_netfilter,xt_socket
  raw.lxc: |
    lxc.apparmor.profile=unconfined
    lxc.mount.auto=proc:rw sys:rw cgroup:rw
    lxc.cgroup.devices.allow=a
    lxc.cap.drop=
  security.nesting: "true"
  security.privileged: "true"
devices:
  aadisable:
    path: /dev/kmsg
    source: /dev/kmsg
    type: unix-char
