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
            charm=self.charm, relation_name=endpoint, prefix="/", mtls_cert=None
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
    def relation(self) -> Relation | None:
        """Return the etcd relation if present."""
        return self.model.get_relation(self.endpoint)

    def get_connection_string(self) -> str:
        """Return the connection string for etcd."""
        if self.relation:
            return self.charmed_etcd.fetch_relation_field(self.relation.id, "endpoints") or ""
        return ""

    def get_client_credentials(self) -> dict[str, str | None]:
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
