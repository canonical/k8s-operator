# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Bootstrap configuration options."""

import collections
import dataclasses
import ipaddress
import logging
from typing import List, Optional

import charms.contextual_status as context_status
import k8sd_api_manager as k8sd
import ops
from literals import (
    BOOTSTRAP_CERTIFICATES,
    BOOTSTRAP_DATASTORE,
    BOOTSTRAP_NODE_TAINTS,
    BOOTSTRAP_POD_CIDR,
    BOOTSTRAP_SERVICE_CIDR,
    CLUSTER_RELATION,
    CLUSTER_WORKER_RELATION,
    DATASTORE_NAME_MAPPING,
    DEFAULT_CERTIFICATE_PROVIDER,
    SUPPORTED_CERTIFICATES,
)
from ops.interface_kube_control.model import Taint
from protocols import K8sCharmProtocol
from pydantic import TypeAdapter, ValidationError

log = logging.getLogger(__name__)


@dataclasses.dataclass
class ConfigOptions:
    """Charm options that has a `bootstrap-` prefix."""

    datastore: Optional[str] = dataclasses.field(
        default=None, metadata={"option": BOOTSTRAP_DATASTORE}
    )
    certificates: str = dataclasses.field(default="")
    pod_cidr: Optional[str] = dataclasses.field(
        default=None, metadata={"option": BOOTSTRAP_POD_CIDR}
    )
    service_cidr: Optional[str] = dataclasses.field(
        default=None, metadata={"option": BOOTSTRAP_SERVICE_CIDR}
    )


def valid_cidr(
    cidr: str, name: str, required: bool = False
) -> set[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Validate a CIDR block.

    Args:
        cidr: The CIDR block to validate.
        name: The name of the configuration option for logging.
        required: Whether the CIDR block is required.

    Raises:
        ValueError: If the CIDR block is invalid.
    """
    if not cidr and required:
        raise ValueError(f"{name} is required.")

    user_cidr: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidr.strip().split(","):
        if not cidr.strip():
            continue
        net = ipaddress.ip_network(cidr)  # Validate the CIDR format.
        if net.prefixlen == net.max_prefixlen:
            raise ValueError(f"CIDR '{cidr}' is a single IP address.")
        user_cidr.append(net)

    if not (1 <= len(user_cidr) <= 2):
        raise ValueError(f"{name} must contain 1 or 2 CIDR blocks, not {len(user_cidr)}.")

    if len(user_cidr) == 2:
        counter = collections.Counter(ipaddress.ip_network(c).version for c in user_cidr)
        if counter[4] != 1 or counter[6] != 1:
            raise ValueError(f"{name} must contain one IPv4 and one IPv6 CIDR block.")
    return set(user_cidr)


def _load_certificates_provider(charm: K8sCharmProtocol) -> str:
    """Load the certificate provider name from the cluster relation.

    Args:
        charm: An instance of the charm.

    Returns:
        str: The certificate provider name.
    """
    app = charm.is_control_plane and charm.app or None
    relation = charm.model.get_relation(CLUSTER_RELATION)
    provider = relation and relation.data[app or relation.app].get(BOOTSTRAP_CERTIFICATES.name)
    return provider or ""


def _persist_certificates_provider(charm: K8sCharmProtocol, provider: str) -> None:
    """Persist the certificates provider in the cluster relation.

    Write-once into the cluster relation at the cluster-certificates key.

    Args:
        charm: An instance of the charm.
        provider (str): The certificates provider to persist.
    """
    peers = charm.model.relations[CLUSTER_RELATION]
    workers = charm.model.relations.get(CLUSTER_WORKER_RELATION, [])
    for relation in peers + workers:
        app_data = relation.data[charm.app]
        if not app_data.get(BOOTSTRAP_CERTIFICATES.name):
            app_data[BOOTSTRAP_CERTIFICATES.name] = provider


class Controller:
    """A store for bootstrap configuration options."""

    def __init__(self, charm: K8sCharmProtocol) -> None:
        """Initialize the BootstrapStore instance.

        Args:
            charm: The charm instance.
        """
        self._charm = charm
        self.immutable = self.load_immutable()

    def load_immutable(self) -> ConfigOptions:
        """Load the bootstrap immutable storage options.

        Args:
            charm: The charm instance.

        Returns:
            A BootstrapStore instance with the configuration options.
        """
        opts = ConfigOptions()
        opts.certificates = _load_certificates_provider(self._charm)

        # Load from the immutable cluster storage.
        if self._charm.is_worker:
            log.debug("Loaded immutable config for worker")
            return opts

        if not self._charm.api_manager.is_cluster_bootstrapped():
            log.debug("Cluster not bootstrapped, no immutable cluster config")
            return opts

        try:
            cluster = self._charm.api_manager.get_cluster_config().metadata
            snap_ds = cluster.datastore and cluster.datastore.type
            opts.datastore = {v: k for k, v in DATASTORE_NAME_MAPPING.items()}.get(snap_ds)
            opts.pod_cidr = cluster.pod_cidr
            opts.service_cidr = cluster.service_cidr
        except (k8sd.K8sdConnectionError, k8sd.InvalidResponseError) as e:
            log.warning("Failed to load cluster config: %s", e)
            # Still return the options we have so far -- they will be validated later.

        return opts

    @property
    def config(self) -> ConfigOptions:
        """Return the current bootstrap configuration options."""
        immutable, juju = self.immutable, self._juju
        return ConfigOptions(
            datastore=immutable.datastore or juju.datastore,
            certificates=immutable.certificates or juju.certificates,
            pod_cidr=immutable.pod_cidr or juju.pod_cidr,
            service_cidr=immutable.service_cidr or juju.service_cidr,
        )

    def validate(self) -> None:
        """Validate the bootstrap options."""
        config = self.config
        try:
            if config.datastore not in DATASTORE_NAME_MAPPING:
                name = BOOTSTRAP_DATASTORE.name
                drop_none = DATASTORE_NAME_MAPPING.keys() - {None}
                log.error(
                    "Invalid %s: %s. Valid Options are: %s",
                    name,
                    config.datastore,
                    ", ".join(sorted(drop_none)),
                )
                raise ValueError(f"{name}='{config.datastore}' is invalid.")
            if config.certificates not in SUPPORTED_CERTIFICATES:
                name = BOOTSTRAP_CERTIFICATES.name
                log.error(
                    "Invalid %s: %s. Valid Options are: %s",
                    name,
                    config.certificates,
                    ", ".join(sorted(SUPPORTED_CERTIFICATES)),
                )
                # TODO: eventually re-enable this error when certificates feature is stable
                # raise ValueError(f"{name}='{config.certificates}' is invalid.")
            if self._charm.is_worker:
                return  # Workers do not validate CIDRs.
            if (cidr := config.service_cidr) or config.certificates == "external":
                required = config.certificates == "external"
                valid_cidr(cidr or "", BOOTSTRAP_SERVICE_CIDR.name, required)
            if cidr := config.pod_cidr:
                valid_cidr(cidr, BOOTSTRAP_POD_CIDR.name)
        except ValueError as e:
            m = str(e)
            log.error("Invalid bootstrap configuration: %s", m)
            context_status.add(ops.BlockedStatus(m))
            raise context_status.ReconcilerError(m) from e

    def persist(self) -> None:
        """Persist the bootstrap configuration options."""
        config = self.config
        self.immutable.datastore = config.datastore
        self.immutable.pod_cidr = config.pod_cidr
        self.immutable.service_cidr = config.service_cidr
        self.immutable.certificates = self.persist_certificates()

    def persist_certificates(self) -> str:
        """Persist the certificates provider in the cluster relation."""
        if not (provider := self.config.certificates):
            raise context_status.ReconcilerError("Missing certificates provider")

        _persist_certificates_provider(self._charm, provider)
        return provider

    @property
    def _juju(self) -> ConfigOptions:
        """Return the bootstrap configuration options from the juju config with auto-mapping.

        Options are always loaded from the charm config, or mapped through the default
        if they are set to "".
        """
        juju, empty = ConfigOptions(), ""
        if self._charm.is_control_plane:
            # Default to self-signed only if the charm is a control plane.
            juju.certificates = DEFAULT_CERTIFICATE_PROVIDER
            if (val := BOOTSTRAP_DATASTORE.get(self._charm)) != empty:
                juju.datastore = val
            if (val := BOOTSTRAP_POD_CIDR.get(self._charm)) != empty:
                juju.pod_cidr = val
            if (val := BOOTSTRAP_SERVICE_CIDR.get(self._charm)) != empty:
                juju.service_cidr = val

        return juju

    def prevent(self) -> Optional[ops.BlockedStatus]:
        """Prevent bootstrap config changes after bootstrap."""
        if not self._charm.is_control_plane:
            # Only control plane nodes can have immutable bootstrap config.
            return None
        blockers = []
        for field in dataclasses.fields(self.immutable):
            cur_val = getattr(self.immutable, field.name)
            option = field.metadata.get("option")
            if cur_val is None or option is None:
                # If the current value is None, it means it was never set.
                log.info("Skipping immutability check for %s==None", field.name)
                continue

            if not (juju := option.get(self._charm)):
                # Auto-mapped options are not immutable.
                log.info("Skipping immutability check for %s=''", field.name)
                continue
            if cur_val != juju:
                log.warning(
                    "Cannot satisfy configuration %s='%s'. Run `juju config %s %s='%s'`",
                    option.name,
                    juju,
                    self._charm.app.name,
                    option.name,
                    cur_val,
                )
                msg = f"{option.name} is immutable; revert to '{cur_val}'"
                blockers.append(ops.BlockedStatus(msg))
        return next(iter(blockers), None)


@context_status.on_error(
    ops.BlockedStatus("Invalid config on bootstrap-node-taints"),
    ValidationError,
)
def node_taints(charm: ops.CharmBase) -> List[str]:
    """Share node taints with the kube-control interface."""
    taints = BOOTSTRAP_NODE_TAINTS.get(charm).split()
    for taint in taints:
        TypeAdapter(Taint).validate_python(taint)
    return taints
