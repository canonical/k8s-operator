# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Interface for Charmed etcd requires relation.

The `CharmedEtcdRequires` class provides an interface to interact with the Charmed etcd relation.
It allows the charm to check the status of the relation, retrieve connection details,
and manage client credentials for etcd.
It is designed to work with the `EtcdRequiresProtocol` and integrates with TLS certificates to
acquire client credentials for secure communication with etcd.
"""

import logging
from typing import Optional

from charms.data_platform_libs.v0.data_interfaces import EtcdRequires
from charms.kubernetes_libs.v0.etcd import EtcdRequiresProtocol
from charms.tls_certificates_interface.v4.tls_certificates import TLSCertificatesRequiresV4
from ops import Relation

log = logging.getLogger(__name__)


class CharmedEtcdRequires(EtcdRequiresProtocol):
    """Charmed etcd requires interface.

    This class is a translation interface that wraps the requires side
    of the charmed etcd interface.
    """

    def __init__(self, charm, etcd_certificate: TLSCertificatesRequiresV4, endpoint="etcd-client"):
        super().__init__(charm, endpoint)

        self.etcd_certificate = etcd_certificate
        self.charmed_etcd = EtcdRequires(
            charm=self.charm,
            relation_name=endpoint,
            prefix="/",
            mtls_cert=self.tls_client_certificate,
        )

    @property
    def is_ready(self) -> bool:
        """Check if the relation is available and emit the appropriate event."""
        return (
            self.relation is not None
            and self.charmed_etcd.fetch_relation_field(self.relation.id, "username") is not None
            and self.charmed_etcd.fetch_relation_field(self.relation.id, "uris") is not None
            and self.charmed_etcd.fetch_relation_field(self.relation.id, "endpoints") is not None
            and self.charmed_etcd.fetch_relation_field(self.relation.id, "tls-ca") is not None
            and self.charmed_etcd.fetch_relation_field(self.relation.id, "version") is not None
        )

    @property
    def relation(self) -> Optional[Relation]:
        """Return the etcd relation if present."""
        return self.model.get_relation(self.endpoint)

    def get_connection_string(self) -> str:
        """Return the connection string for etcd."""
        if self.relation:
            return self.charmed_etcd.fetch_relation_field(self.relation.id, "uris") or ""
        return ""

    def get_client_credentials(self) -> dict[str, Optional[str]]:
        """Return the client credentials for etcd."""
        certificates, private_key = self.etcd_certificate.get_assigned_certificates()
        if not self.relation or not certificates or not private_key:
            log.warning("No etcd client credentials available.")
            return {
                "client_cert": None,
                "client_key": None,
                "client_ca": None,
            }

        client_cert = certificates[0].certificate.raw
        client_key = private_key.raw
        client_ca = certificates[0].ca.raw

        self.charmed_etcd.set_mtls_cert(self.relation.id, client_cert)

        return {
            "client_cert": client_cert,
            "client_key": client_key,
            "client_ca": client_ca,
        }

    @property
    def tls_client_certificate(self) -> Optional[str]:
        """Return the client certificate for etcd."""
        certificates, _ = self.etcd_certificate.get_assigned_certificates()
        return certificates[0].certificate.raw if certificates else None

    def update_relation_data(self):
        """Update the relation data with the current state."""
        if not self.relation:
            log.warning("No etcd relation to update.")
            return
        if not self.tls_client_certificate:
            log.warning("No TLS client certificate available to update relation data.")
            return
        log.debug(f"Updating relation data for etcd with relation ID {self.relation.id}.")
        self.charmed_etcd.set_mtls_cert(self.relation.id, self.tls_client_certificate)
