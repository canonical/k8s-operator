# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""K8s Certificates module."""

import ipaddress
import logging
from typing import Dict, List, Set, Tuple, Union

import charms.contextual_status as status
import ops
from charms.k8s.v0.k8sd_api_manager import (
    BootstrapConfig,
    ControlPlaneNodeJoinConfig,
    NodeJoinConfig,
)
from charms.tls_certificates_interface.v4.tls_certificates import (
    CertificateRequestAttributes,
    Mode,
    PrivateKey,
    ProviderCertificate,
    TLSCertificatesRequiresV4,
)
from literals import CONTROL_PLANE_CERTIFICATES, SUPPORTED_CERTIFICATES, WORKER_CERTIFICATES

log = logging.getLogger(__name__)


class RefreshCertificates(ops.EventBase):
    """Event emitted when the certificates need to be refreshed."""


class K8sCertificates:
    """A class for managing Kubernetes certificates associated with a cluster unit.

    Attributes:
        events (List[str]): A list of events emitted by the Certificates library.
        using_external_certificates (bool): Whether the charm is using external certificates.
    """

    def __init__(self, charm, refresh_event: ops.EventSource) -> None:
        """Initialize the K8sCertificates class.

        Args:
            charm: An instance of the charm.
            refresh_event: An event source that triggers certificate refresh.
        """
        self.charm = charm
        self.model = charm.model
        self.config = charm.config
        if self.using_external_certificates:
            self.certificates = TLSCertificatesRequiresV4(
                charm=self.charm,
                relationship_name="certificates",
                certificate_requests=self._certificate_requests,
                mode=Mode.UNIT,
                refresh_events=[refresh_event],
            )

    @property
    def using_external_certificates(self) -> bool:
        """Return whether the charm is using external certificates."""
        return self.config.get("bootstrap-certificates") == "external"

    @property
    def events(self) -> List[ops.CharmEvents]:
        """Return the events that the Certificates library emits."""
        return (
            [self.certificates.on.certificate_available]
            if self.using_external_certificates
            else []
        )

    @property
    def _certificate_requests(self) -> List[CertificateRequestAttributes]:
        """Generate certificates requests for all supported components."""
        return list(self._certificates_request_mapping.values())

    @property
    def _certificates_request_mapping(
        self,
    ) -> Dict[str, CertificateRequestAttributes]:
        node_name = self.charm.get_node_name()
        sans_ip = {"127.0.0.1", "::1"}
        sans_dns = {
            node_name,
            "kubernetes",
            "kubernetes.default",
            "kubernetes.default.svc",
            "kubernetes.default.svc.cluster.local",
        }

        if self.charm.is_control_plane:
            sans_ip.update(self._get_service_ips())

        extra_ips, extra_dns = self.charm.get_sorted_sans()
        sans_ip.update(extra_ips)
        sans_dns.update(extra_dns)

        csr_attributes = {
            "admin": CertificateRequestAttributes(
                common_name="kubernetes:admin",
                organization="system:masters",
            ),
            "controller": CertificateRequestAttributes(
                common_name="system:kube-controller-manager",
            ),
            "scheduler": CertificateRequestAttributes(
                common_name="system:kube-scheduler",
            ),
            "proxy": CertificateRequestAttributes(
                common_name="system:kube-proxy",
            ),
            "apiserver": CertificateRequestAttributes(
                common_name="kube-apiserver",
                sans_dns=frozenset(sans_dns),
                sans_ip=frozenset(sans_ip),
            ),
            "front-proxy-client": CertificateRequestAttributes(
                common_name="front-proxy-client",
            ),
            "kubelet": CertificateRequestAttributes(
                common_name=f"system:node:{node_name}",
                organization="system:nodes",
                sans_dns=frozenset(sans_dns),
                sans_ip=frozenset(sans_ip),
            ),
            "kubelet-client": CertificateRequestAttributes(
                common_name=f"system:node:{node_name}",
                organization="system:nodes",
            ),
            "apiserver-kubelet-client": CertificateRequestAttributes(
                common_name=f"system:node:{node_name}",
                organization="system:nodes",
            ),
        }

        if self.charm.lead_control_plane:
            return csr_attributes
        if self.charm.is_control_plane:
            return {k: v for k, v in csr_attributes.items() if k in CONTROL_PLANE_CERTIFICATES}
        return {k: v for k, v in csr_attributes.items() if k in WORKER_CERTIFICATES}

    def _get_service_ips(self) -> Set[str]:
        """Get Kubernetes service IPs from the CIDRs.

        Returns:
            Set[str]: A set of Kubernetes service IPs.

        Raises:
            ValueError: If the service CIDR is invalid.
        """
        service_ips = set()
        service_cidrs = self.config["bootstrap-service-cidr"].split(",")
        for cidr in service_cidrs:
            cidr = cidr.strip()
            try:
                network = ipaddress.ip_network(cidr)
                service_ips.add(str(network[1]))
            except ValueError:
                log.exception("Invalid service CIDR: %s", cidr)
                raise
        return service_ips

    def _get_validated_certificate(
        self, request: CertificateRequestAttributes
    ) -> Tuple[ProviderCertificate, PrivateKey]:
        """Get and validate a certificate/key pair for the given request.

        Args:
            request (CertificateRequestAttributes): The certificate request attributes.

        Returns:
            Tuple[ProviderCertificate, PrivateKey]: A tuple containing the certificate and key.

        Raises:
            ReconcilerError: If the certificate/key pair is missing.
        """
        certificate, key = self.certificates.get_assigned_certificate(request)
        if not certificate or not key:
            raise status.ReconcilerError(f"Missing certificate/key pair for {request.common_name}")

        return certificate, key

    def _populate_join_certificates(self, config: NodeJoinConfig) -> None:
        """Configure the provided NodeJoinConfig certificates.

        Args:
            config (NodeJoinConfig): An instance of NodeJoinConfig where the
                certificates and keys will be stored.
        """
        attrs = self._certificates_request_mapping

        if isinstance(config, ControlPlaneNodeJoinConfig):
            certificate, key = self._get_validated_certificate(attrs["apiserver"])
            config.apiserver_crt = str(certificate.certificate)
            config.apiserver_key = str(key)

            certificate, key = self._get_validated_certificate(attrs["front-proxy-client"])
            config.front_proxy_client_crt = str(certificate.certificate)
            config.front_proxy_client_key = str(key)

            certificate, key = self._get_validated_certificate(attrs["admin"])
            config.admin_client_cert = str(certificate.certificate)
            config.admin_client_key = str(key)

            certificate, key = self._get_validated_certificate(attrs["scheduler"])
            config.kube_scheduler_client_cert = str(certificate.certificate)
            config.kube_scheduler_client_key = str(key)

            certificate, key = self._get_validated_certificate(attrs["controller"])
            config.kube_controller_manager_client_cert = str(certificate.certificate)
            config.kube_controller_manager_client_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["kubelet"])
        config.kubelet_cert = str(certificate.certificate)
        config.kubelet_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["kubelet-client"])
        config.kubelet_client_cert = str(certificate.certificate)
        config.kubelet_client_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["proxy"])
        config.kube_proxy_client_cert = str(certificate.certificate)
        config.kube_proxy_client_key = str(key)

    def _populate_bootstrap_certificates(self, bootstrap_config: BootstrapConfig) -> None:
        """Configure the provided BootstrapConfig certificates.

        Args:
            bootstrap_config (BootstrapConfig): An instance of BootstrapConfig
                where the certificates and keys will be stored.
        """
        attrs = self._certificates_request_mapping

        certificate, key = self._get_validated_certificate(attrs["apiserver"])
        bootstrap_config.ca_cert = str(certificate.ca)
        bootstrap_config.client_ca_cert = str(certificate.ca)
        bootstrap_config.api_server_cert = str(certificate.certificate)
        bootstrap_config.api_server_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["front-proxy-client"])
        bootstrap_config.front_proxy_ca_cert = str(certificate.ca)
        bootstrap_config.front_proxy_client_cert = str(certificate.certificate)
        bootstrap_config.front_proxy_client_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["kubelet"])
        bootstrap_config.kubelet_cert = str(certificate.certificate)
        bootstrap_config.kubelet_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["kubelet-client"])
        bootstrap_config.kubelet_client_cert = str(certificate.certificate)
        bootstrap_config.kubelet_client_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["apiserver-kubelet-client"])
        bootstrap_config.api_server_kubelet_client_cert = str(certificate.certificate)
        bootstrap_config.api_server_kubelet_client_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["admin"])
        bootstrap_config.admin_client_cert = str(certificate.certificate)
        bootstrap_config.admin_client_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["controller"])
        bootstrap_config.kube_controller_manager_client_cert = str(certificate.certificate)
        bootstrap_config.kube_controller_manager_client_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["scheduler"])
        bootstrap_config.kube_scheduler_client_cert = str(certificate.certificate)
        bootstrap_config.kube_scheduler_client_key = str(key)

        certificate, key = self._get_validated_certificate(attrs["proxy"])
        bootstrap_config.kube_proxy_client_cert = str(certificate.certificate)
        bootstrap_config.kube_proxy_client_key = str(key)

    def configure_certificates(self, config: Union[BootstrapConfig, NodeJoinConfig]):
        """Configure the certificates for the Kubernetes cluster.

        Args:
            config (BootstrapConfig):
                The configuration object for the Kubernetes cluster. This object
                will be modified in-place to include the cluster's certificates.

        Raises:
            ReconcilerError: If the certificates issuer is invalid.
        """
        certificates_type = self.config.get("bootstrap-certificates")

        if certificates_type not in SUPPORTED_CERTIFICATES:
            log.error(
                "Unsupported certificate issuer: %s. Valid options: %s",
                certificates_type,
                ", ".join(SUPPORTED_CERTIFICATES),
            )
            status.add(ops.BlockedStatus(f"Invalid certificates issuer: {certificates_type}"))
            raise status.ReconcilerError("Invalid certificates issuer")

        if certificates_type == "external":
            log.info("Using external certificates")
            certificates_relation = self.model.get_relation("certificates")
            if not certificates_relation:
                msg = "Missing required 'certificates' relation"
                status.add(ops.BlockedStatus(msg))
                raise status.ReconcilerError(msg)

            if self.charm.lead_control_plane and isinstance(config, BootstrapConfig):
                self._populate_bootstrap_certificates(config)
            elif self.charm.is_control_plane and isinstance(config, ControlPlaneNodeJoinConfig):
                self._populate_join_certificates(config)
            elif isinstance(config, NodeJoinConfig):
                self._populate_join_certificates(config)
