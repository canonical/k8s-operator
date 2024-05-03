# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""K8s Certificates module."""
import logging
from typing import List

import ops
import utils

from charms.k8s.v0.k8sd_api_manager import BootstrapConfig
from charms.tls_certificates_interface.v3.tls_certificates import (
    CertificateAvailableEvent,
    generate_csr,
    generate_private_key,
)

log = logging.getLogger(__name__)

WORKER_COMPONENTS = ["kubelet"]
CONTROL_PLANE_COMPONENTS = ["apiserver", "front-proxy-client"] + WORKER_COMPONENTS
LEADER_CONTROL_PLANE_COMPONENTS = ["apiserver-kubelet-client"] + CONTROL_PLANE_COMPONENTS


class K8sCertificates:
    """A class for managing Kubernetes certificates associated with a cluster unit."""

    def __init__(self, charm, certificates) -> None:
        """Initialize the K8sCertificates class.

        Args:
            charm: An instance of the charm.
            certificates: An instance of the TLSCertificatesv3 requirer library.
        """
        self.charm = charm
        self.certificates = certificates

    def _check_secret(self, label: str):
        """Check if a secret with the specified label exists in Juju."""
        try:
            self.charm.model.get_secret(label=label)
            return True
        except ops.SecretNotFoundError:
            return False

    def _generate_private_key(self, components: List[str]):
        """Generate private keys for each component and store them as secrets."""
        content = dict()

        for key in components:
            private_key = generate_private_key()
            content[key] = private_key.decode()

        self.charm.unit.add_secret(content=content, label=f"{self.charm.unit.name}-private-keys")
        log.info(f"Initialized {self.charm.unit.name} private keys.")

    def _generate_csr(self):
        """Generate Certificate Signing Requests for specified components."""
        private_key_secret = self.charm.model.get_secret(
            label=f"{self.charm.unit.name}-private-keys"
        )
        private_key_secret_content = private_key_secret.get_content(refresh=True)
        components = set(private_key_secret_content.keys())

        content = dict()

        address = utils.get_public_address()
        node_name = self.charm.get_node_name()

        if "apiserver" in components:
            api_server_csr = generate_csr(
                private_key=private_key_secret_content["apiserver"].encode(),
                subject="kube-apiserver",
                sans_dns=[
                    self.charm.get_node_name(),
                    "kubernetes",
                    "kubernetes.default",
                    "kubernetes.default.svc",
                    "kubernetes.default.svc.cluster.local",
                ],
                sans_ip=[address, "127.0.0.1", "10.152.183.1"],
            )

            content["apiserver-csr"] = api_server_csr.decode()
            log.info("API Server CSR generated")

        if "front-proxy-client" in components:
            front_proxy = generate_csr(
                private_key=private_key_secret_content["front-proxy-client"].encode(),
                subject="front-proxy-client",
            )

            content["front-proxy-client-csr"] = front_proxy.decode()
            log.info("Front Proxy CSR generated")

        if "kubelet" in components:
            kubelet_csr = generate_csr(
                private_key=private_key_secret_content["kubelet"].encode(),
                subject=f"system:node:{node_name}",
                organization="system:nodes",
                sans_dns=[
                    node_name,
                    "kubernetes",
                    "kubernetes.default",
                    "kubernetes.default.svc",
                    "kubernetes.default.svc.cluster.local",
                ],
                sans_ip=[address, "127.0.0.1", "10.152.183.1"],
            )

            content["kubelet-csr"] = kubelet_csr.decode()
            log.info("Kubelet CSR generated")

        if "apiserver-kubelet-client" in components:
            apiserver_client = generate_csr(
                private_key=private_key_secret_content["apiserver-kubelet-client"].encode(),
                subject=f"system:node:{node_name}",
                organization="system:nodes",
            )

            content["apiserver-kubelet-client-csr"] = apiserver_client.decode()
            log.info("API Server Client CSR generated")

        self.charm.unit.add_secret(content=content, label=f"{self.charm.unit.name}-csr")

    def _generate_certificate(self, components: List[str]):
        """Request the creation of certificates for components lacking a certificate."""
        csr = self.charm.model.get_secret(label=f"{self.charm.unit.name}-csr")
        csr_content = csr.get_content(refresh=True)

        for component in components:
            if not self._check_secret(f"{component}-cert-{self.charm.unit.name}"):
                self.certificates.request_certificate_creation(
                    certificate_signing_request=csr_content[f"{component}-csr"].encode()
                )
                log.info(f"{component} Certificate Requested")

    def collect_certificate(self, event: CertificateAvailableEvent):
        """Collect and store a certificate.

        This method handles the CertificateAvailableEvent by verifying if the
        CSR from the event matches any stored CSRs. If a match is found, it
        updates or creates a secret with the certificate, CA certificate, and
        the CSR.

        Parameters:
        event (CertificateAvailableEvent): An event that triggers the certificate
                                          collection, containing the certificate,
                                          CA certificate, and CSR.
        """
        if not isinstance(event, CertificateAvailableEvent):
            return

        if not self._check_secret(f"{self.charm.unit.name}-csr"):
            return

        csr = self.charm.model.get_secret(label=f"{self.charm.unit.name}-csr")
        csr_content = csr.get_content(refresh=True)
        csr_keys = set(csr_content.keys())

        for csr in csr_keys:
            if csr_content[csr].strip() != event.certificate_signing_request:
                continue
            certificate_content = {
                "certificate": event.certificate,
                "ca-certificate": event.ca,
                "csr": event.certificate_signing_request,
            }
            name = csr.replace("-csr", "")
            if self._check_secret(label=f"{name}-cert-{self.charm.unit.name}"):
                certificate_secret = self.charm.model.get_secret(
                    label=f"{name}-cert-{self.charm.unit.name}"
                )
                certificate_secret.set_content(content=certificate_content)
            else:
                self.charm.unit.add_secret(
                    content=certificate_content, label=f"{name}-cert-{self.charm.unit.name}"
                )
            log.info(f"New certificate stored for {name}: {event.certificate}")
            break
        else:
            log.warning("Event CSR does not match any stored CSR")

    def _ensure_complete_certificates(self, components: List[str]):
        """Ensure the certificates are complete for the unit."""
        certs = dict()
        for component in components:
            assert self._check_secret(
                f"{component}-cert-{self.charm.unit.name}"
            ), f"Missing {component} cert"
            cert_secret = self.charm.model.get_secret(
                label=f"{component}-cert-{self.charm.unit.name}"
            )
            cert_content = cert_secret.get_content(refresh=True)
            certs[component] = cert_content

    def generate_bootstrap_certificates(self, bootstrap_config: BootstrapConfig):
        """Configure the provided BootstrapConfig certificates.

        This method gathers necessary certificates and private keys for the
        leader control plane components and assigns them to the respective
        attributes of the BootstrapConfig object.

        Args:
            bootstrap_config (BootstrapConfig): An instance of BootstrapConfig
                where the certificates and keys will be stored.
        """
        components = LEADER_CONTROL_PLANE_COMPONENTS
        certificates = self._get_unit_certificates(components)
        pk_secret = self.charm.model.get_secret(label=f"{self.charm.unit.name}-private-keys")
        pk_content = pk_secret.get_content(refresh=True)

        bootstrap_config.ca_cert = certificates["apiserver"]["ca-certificate"]

        bootstrap_config.apiserver_crt = certificates["apiserver"]["certificate"]
        bootstrap_config.apiserver_key = pk_content["apiserver"]

        bootstrap_config.kubelet_crt = certificates["kubelet"]["certificate"]
        bootstrap_config.kubelet_key = pk_content["kubelet"]

        bootstrap_config.front_proxy_ca_cert = certificates["front-proxy-client"]["ca-certificate"]
        bootstrap_config.front_proxy_client_cert = certificates["front-proxy-client"][
            "certificate"
        ]
        bootstrap_config.front_proxy_client_key = pk_content["front-proxy-client"]

        bootstrap_config.service_account_key = pk_content["service-account"]

        bootstrap_config.apiserver_kubelet_client_crt = certificates["apiserver-kubelet-client"][
            "certificate"
        ]
        bootstrap_config.apiserver_kubelet_client_key = pk_content["apiserver-kubelet-client"]

    def _get_unit_certificates(self, components: List[str]):
        """Generate the private keys, CSRs and certificates for the unit."""
        if not self._check_secret(f"{self.charm.unit.name}-private-keys"):
            self._generate_private_key(
                components + ["service-account"] if self.charm.lead_control_plane else components
            )
        if not self._check_secret(f"{self.charm.unit.name}-csr"):
            self._generate_csr()
        self._generate_certificate(components)

        certificates = dict()
        for component in components:
            assert self._check_secret(
                f"{component}-cert-{self.charm.unit.name}"
            ), f"Missing {component} certificate"
            cert_secret = self.charm.model.get_secret(
                label=f"{component}-cert-{self.charm.unit.name}"
            )
            cert_content = cert_secret.get_content(refresh=True)
            certificates[component] = cert_content

        return certificates
