# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""K8s Certificates module."""

import ipaddress
import logging
from string import Template
from typing import Dict, List, Optional, Protocol, Set, Tuple, Union, cast

import ops
from literals import (
    APISERVER_CN_FORMATTER_CONFIG_KEY,
    APISERVER_CSR_KEY,
    CERTIFICATES_RELATION,
    CLUSTER_CERTIFICATES_DOMAIN_NAME_KEY,
    CLUSTER_CERTIFICATES_KEY,
    CLUSTER_CERTIFICATES_KUBELET_FORMATTER_KEY,
    CLUSTER_RELATION,
    KUBELET_CN_FORMATTER_CONFIG_KEY,
    KUBELET_CSR_KEY,
    MAX_COMMON_NAME_SIZE,
    SUPPORTED_CERTIFICATES,
)
from protocols import K8sCharmProtocol

import charms.contextual_status as status
from charms.k8s.v0.k8sd_api_manager import (
    BootstrapConfig,
    ControlPlaneNodeJoinConfig,
    NodeJoinConfig,
)
from charms.tls_certificates_interface.v4.tls_certificates import (
    CertificateRequestAttributes,
    CertificatesRequirerCharmEvents,
    Mode,
    PrivateKey,
    ProviderCertificate,
    TLSCertificatesRequiresV4,
)

log = logging.getLogger(__name__)


class CertificateProvider(Protocol):
    """Protocol defining the interface for certificate providers."""

    @property
    def on(self) -> ops.ObjectEvents:
        """Certificate events."""
        ...

    def get_assigned_certificate(
        self, certificate_request: CertificateRequestAttributes
    ) -> Tuple[Optional[ProviderCertificate], Optional[PrivateKey]]:
        """Get a certificate/key pair for the given request."""
        ...


class NoOpCertificateProvider:
    """No-Op certificate provider for when external certificates are not in use."""

    on = CertificatesRequirerCharmEvents()

    def get_assigned_certificate(
        self, certificate_request: CertificateRequestAttributes
    ) -> Tuple[ProviderCertificate, PrivateKey]:
        """Raise an error as this method should never be called."""
        # NOTE: This should never be called as the self-signed certificates will be
        # generated by the snap.
        raise status.ReconcilerError(
            f"No certificate provider available for {certificate_request.common_name}"
        )


class RefreshCertificates(ops.EventBase):
    """Event emitted when the certificates need to be refreshed."""


class K8sCertificates(ops.Object):
    """A class for managing Kubernetes certificates associated with a cluster unit.

    Attributes:
        events (List[str]): A list of events emitted by the Certificates library.
        using_external_certificates (bool): Whether the charm is using external certificates.
    """

    def __init__(self, charm: K8sCharmProtocol, refresh_event: ops.BoundEvent) -> None:
        """Initialize the K8sCertificates class.

        Args:
            charm: An instance of the charm.
            refresh_event: An event source that triggers certificate refresh.
        """
        super().__init__(charm, "certificates-integration")
        self._charm = charm
        self._refresh_event = refresh_event
        self._certificates = self._init_certificates_provider()

    def _init_certificates_provider(self) -> CertificateProvider:
        """Return a certificate provider based on the charm configuration.

        Args:
            refresh_event: An event source that triggers certificate refresh.

        Returns:
            CertificateProvider: An instance implementing the CertificateProvider protocol.
        """
        if self._get_certificates_provider() == "external":
            return TLSCertificatesRequiresV4(
                charm=self._charm,
                relationship_name=CERTIFICATES_RELATION,
                certificate_requests=self._certificate_requests,
                mode=Mode.UNIT,
                refresh_events=[self._refresh_event],
            )

        return NoOpCertificateProvider()

    @property
    def apiserver_common_name(self) -> str:
        """API Server common name."""
        formatter = str(self._charm.config.get(APISERVER_CN_FORMATTER_CONFIG_KEY, ""))
        return self._format_common_name(formatter)

    @property
    def kubelet_common_name(self) -> str:
        """Kubelet common name."""
        formatter = ""
        if self._charm.is_control_plane:
            formatter = str(self._charm.config.get(KUBELET_CN_FORMATTER_CONFIG_KEY, ""))
        else:
            relation = self.model.get_relation(CLUSTER_RELATION)
            formatter = (
                str(relation.data[relation.app].get(CLUSTER_CERTIFICATES_KUBELET_FORMATTER_KEY))
                if relation and relation.app in relation.data
                else ""
            )

        return self._format_common_name(formatter)

    @property
    def domain_name(self) -> str:
        """Certificates domain name."""
        if self._charm.is_control_plane:
            return str(self._charm.config.get("external-certs-domain-name"))
        else:
            relation = self.model.get_relation(CLUSTER_RELATION)
            return (
                str(relation.data[relation.app].get(CLUSTER_CERTIFICATES_DOMAIN_NAME_KEY))
                if relation and relation.app in relation.data
                else ""
            )

    @property
    def using_external_certificates(self) -> bool:
        """Return whether the charm is using external certificates."""
        return self._get_certificates_provider() == "external"

    @property
    def events(self) -> List[ops.BoundEvent]:
        """Return the events that the Certificates library emits."""
        return (
            [self._certificates.on.certificate_available]
            if isinstance(self._certificates, TLSCertificatesRequiresV4)
            else []
        )

    @property
    def _certificate_requests(self) -> List[CertificateRequestAttributes]:
        """Return the certificate requests.

        If the common name formatters are invalid, return an empty list.
        """
        return list(self._certificate_requests_mapping.values())

    @property
    def _certificate_requests_mapping(self) -> Dict[str, CertificateRequestAttributes]:
        """Return the certificate requests mapping.

        If the common name formatters are invalid, return an empty dict.
        """
        try:
            requests = {}

            requests[KUBELET_CSR_KEY] = self._create_certificates_request(
                common_name=self.kubelet_common_name, organization="system:nodes"
            )

            if self._charm.is_control_plane:
                requests[APISERVER_CSR_KEY] = self._create_certificates_request(
                    common_name=self.apiserver_common_name
                )

            return requests

        except (KeyError, ValueError):
            log.exception("Invalid common name formatter")
            return {}

    def _create_certificates_request(
        self, common_name: str, organization: Optional[str] = None
    ) -> CertificateRequestAttributes:
        node_name = self._charm.get_node_name()
        sans_ip = {"127.0.0.1", "::1"}
        sans_dns = {
            node_name,
            "kubernetes",
            "kubernetes.default",
            "kubernetes.default.svc",
            "kubernetes.default.svc.cluster.local",
        }

        if self._charm.is_control_plane:
            sans_ip.update(self._get_service_ips())

        extra_ips, extra_dns = self._charm.split_sans_by_type()
        sans_ip.update(extra_ips)
        sans_dns.update(extra_dns)
        return CertificateRequestAttributes(
            common_name=common_name,
            organization=organization,
            sans_dns=frozenset(sans_dns),
            sans_ip=frozenset(sans_ip),
        )

    def _format_common_name(self, formatter: str) -> str:
        """Format a common name using the provided formatter."""
        if not formatter:
            raise ValueError("Empty common name formatter")

        tmp_context = {
            "node_name": self._charm.get_node_name(),
            "cluster_name": self._charm.get_cluster_name(),
            "domain_name": self._charm.config.get("external-certs-domain-name"),
        }

        try:
            template = Template(formatter)
            formatted_name = template.safe_substitute(tmp_context)
            if not formatted_name:
                raise ValueError(
                    f"Common name formatter '{formatter}' resulted in '{formatted_name}'"
                )

            return formatted_name
        except ValueError:
            log.exception("Invalid common name formatter '%s'", formatter)
            raise

    def _get_service_ips(self) -> Set[str]:
        """Get Kubernetes service IPs from the CIDRs.

        Returns:
            Set[str]: A set of Kubernetes service IPs.

        Raises:
            ValueError: If the service CIDR is invalid.
        """
        service_ips = set()
        service_cidrs = self._charm.config.get("bootstrap-service-cidr", "")
        if not isinstance(service_cidrs, str):
            log.warning("Service CIDRs is not str, instead %s", type(service_cidrs))
            return set()
        cidrs = service_cidrs.split(",")

        for cidr in cidrs:
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
        """Get and validate a certificate/key pair for a given request.

        Returns:
            Tuple[ProviderCertificate, PrivateKey]: A tuple containing the certificate and key.

        Raises:
            ReconcilerError: If the certificate/key pair is missing.
        """
        certificate, key = self._certificates.get_assigned_certificate(request)
        if not certificate or not key:
            self._refresh_event.emit()
            raise status.ReconcilerError(f"Missing certificate/key pair for {request.common_name}")

        # NOTE: (mateoflorido) Cast to non-None types since we've validated they exist.
        return cast(ProviderCertificate, certificate), cast(PrivateKey, key)

    def _populate_join_certificates(self, config: NodeJoinConfig) -> None:
        """Configure the provided NodeJoinConfig certificates.

        Args:
            config (NodeJoinConfig): An instance of NodeJoinConfig where the
                certificates and keys will be stored.
        """
        if isinstance(config, ControlPlaneNodeJoinConfig):
            certificate, key = self._get_validated_certificate(
                self._certificate_requests_mapping.get(APISERVER_CSR_KEY)
            )
            config.apiserver_crt = str(certificate.certificate)
            config.apiserver_key = str(key)

        certificate, key = self._get_validated_certificate(
            self._certificate_requests_mapping.get(KUBELET_CSR_KEY)
        )
        config.kubelet_cert = str(certificate.certificate)
        config.kubelet_key = str(key)

    def _get_certificate_full_chain(self, certificate: ProviderCertificate):
        """Build the full certificate chain from a ProviderCertificate."""
        return "\n".join(str(cert) for cert in certificate.chain + [certificate.ca])

    def _populate_bootstrap_certificates(self, bootstrap_config: BootstrapConfig) -> None:
        """Configure the provided BootstrapConfig certificates.

        Args:
            bootstrap_config (BootstrapConfig): An instance of BootstrapConfig
                where the certificates and keys will be stored.
        """
        certificate, key = self._get_validated_certificate(
            self._certificate_requests_mapping.get(APISERVER_CSR_KEY)
        )
        bootstrap_config.ca_cert = self._get_certificate_full_chain(certificate)
        bootstrap_config.api_server_cert = str(certificate.certificate)
        bootstrap_config.api_server_key = str(key)

        certificate, key = self._get_validated_certificate(
            self._certificate_requests_mapping.get(KUBELET_CSR_KEY)
        )
        bootstrap_config.kubelet_cert = str(certificate.certificate)
        bootstrap_config.kubelet_key = str(key)

    def _get_certificates_provider(self) -> Optional[str]:
        """Get the certificates provider.

        Returns:
            str: The certificates provider.
        """
        if self._charm.is_control_plane:
            return str(self._charm.config.get("bootstrap-certificates"))

        # NOTE: This operation is safe because we're validating the provider during the
        # certificate configuration in the `configure_certificates` method.
        relation = self.model.get_relation(CLUSTER_RELATION)
        if not relation:
            return None

        if not (provider := relation.data[relation.app].get(CLUSTER_CERTIFICATES_KEY)):
            log.info("Waiting for certificates provider")
            return None

        return provider

    def _validate_common_name_size(self):
        """Validate that the common names do not exceed the maximum size."""
        if self._charm.is_control_plane and len(self.apiserver_common_name) > MAX_COMMON_NAME_SIZE:
            raise status.ReconcilerError(
                f"CN: {self.apiserver_common_name} exceeds {MAX_COMMON_NAME_SIZE} chars."
            )
        if len(self.kubelet_common_name) > MAX_COMMON_NAME_SIZE:
            raise status.ReconcilerError(
                f"CN: {self.kubelet_common_name} exceeds {MAX_COMMON_NAME_SIZE} chars."
            )

    def _validate_common_name_formatters(self):
        """Validate that the common name formatters are valid."""
        try:
            formatters = []
            if self._charm.is_control_plane:
                formatters.append(self.apiserver_common_name)
            formatters.append(self.kubelet_common_name)
            if any(not f for f in formatters):
                msg = "Common name formatters resulted in empty common names"
                raise status.ReconcilerError(msg)
        except (KeyError, ValueError):
            msg = "Invalid common name formatters."
            log.exception(msg)
            status.add(ops.BlockedStatus(msg))
            raise status.ReconcilerError(msg)

    def configure_certificates(self, config: Union[BootstrapConfig, NodeJoinConfig]):
        """Configure the certificates for the Kubernetes cluster.

        Args:
            config (BootstrapConfig):
                The configuration object for the Kubernetes cluster. This object
                will be modified in-place to include the cluster's certificates.

        Raises:
            ReconcilerError: If the certificates issuer is invalid.
        """
        certificates_type = self._get_certificates_provider()

        if certificates_type not in SUPPORTED_CERTIFICATES:
            log.error(
                "Unsupported certificate issuer: %s. Valid options: %s",
                certificates_type,
                ", ".join(SUPPORTED_CERTIFICATES),
            )
            status.add(ops.BlockedStatus(f"Invalid certificates issuer: {certificates_type}"))
            raise status.ReconcilerError("Invalid certificates issuer")

        if certificates_type == "external":
            log.info("Using external certificates for kube-apiserver and kubelet.")
            if not self._charm.model.get_relation(CERTIFICATES_RELATION):
                msg = "Missing required 'certificates' relation"
                status.add(ops.BlockedStatus(msg))
                raise status.ReconcilerError(msg)

            self._validate_common_name_formatters()
            self._validate_common_name_size()

            if not self._certificate_requests:
                msg = "Invalid certificate requests due to common name formatter issues"
                status.add(ops.BlockedStatus(msg))
                raise status.ReconcilerError(msg)

            if self._charm.lead_control_plane and isinstance(config, BootstrapConfig):
                self._populate_bootstrap_certificates(config)
            elif self._charm.is_control_plane and isinstance(config, ControlPlaneNodeJoinConfig):
                self._populate_join_certificates(config)
            elif isinstance(config, NodeJoinConfig):
                self._populate_join_certificates(config)
