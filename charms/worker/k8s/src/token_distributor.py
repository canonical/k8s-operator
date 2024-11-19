# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Token Distributor module."""

import contextlib
import logging
import re
from enum import Enum, auto
from typing import Dict, Optional, Protocol, Union

import charms.contextual_status as status
import ops
from charms.k8s.v0.k8sd_api_manager import (
    ErrorCodes,
    InvalidResponseError,
    K8sdAPIManager,
    K8sdConnectionError,
)
from pydantic import SecretStr

log = logging.getLogger(__name__)

SECRET_ID = "{0}-secret-id"  # nosec

UNIT_RE = re.compile(r"k8s(-worker)?/\d+")


class K8sCharm(Protocol):
    """Typing for the K8sCharm.

    Attributes:
        app (ops.Application): The application object.
        model (ops.Model): The model object.
        unit (ops.Unit): The unit object.
    """

    @property
    def app(self) -> ops.Application:
        """The application object."""
        ...  # pylint: disable=unnecessary-ellipsis

    @property
    def model(self) -> ops.Model:
        """The model object."""
        ...  # pylint: disable=unnecessary-ellipsis

    @property
    def unit(self) -> ops.Unit:
        """The unit object."""
        ...  # pylint: disable=unnecessary-ellipsis

    def get_cluster_name(self) -> str:
        """Get the cluster name."""
        ...  # pylint: disable=unnecessary-ellipsis


class TokenStrategy(Enum):
    """Enumeration defining strategy for token creation.

    Attributes:
        CLUSTER: Strategy for creating cluster tokens.
        COS: Strategy for creating COS tokens.
    """

    CLUSTER = auto()
    COS = auto()


class ClusterTokenType(Enum):
    """Enumeration defining types for cluster tokens.

    Attributes:
        CONTROL_PLANE: Token type for control plane.
        WORKER: Token type for worker nodes.
        NONE: No specific token type.
    """

    CONTROL_PLANE = "control-plane"
    WORKER = "worker"
    NONE = ""


class ClusterTokenManager:
    """Class for managing cluster tokens.

    Attributes:
        allocator_needs_tokens: The allocating node does not need a cluster token to join
        strategy: The cluster strategy for token creation.
        revoke_on_join: Revoke a token once it's joined.
    """

    allocator_needs_tokens: bool = False
    strategy: TokenStrategy = TokenStrategy.CLUSTER
    revoke_on_join = True

    def __init__(self, api_manager: K8sdAPIManager):
        """Initialize a ClusterTokenManager instance.

        Args:
            api_manager (K8sdAPIManager): An K8sdAPIManager object for interacting with k8sd API.
        """
        self.api_manager = api_manager

    def create(self, name: str, token_type: ClusterTokenType) -> SecretStr:
        """Create a cluster token.

        Args:
            name (str): The name of the node.
            token_type (ClusterTokenType): The type of cluster token.

        Returns:
            SecretStr: The created cluster token.
        """
        worker = token_type == ClusterTokenType.WORKER
        return self.api_manager.create_join_token(name, worker=worker)

    def revoke(self, name: str, ignore_errors: bool):
        """Remove a cluster token.

        Args:
            name (str): The name of the node.
            ignore_errors (bool): Whether or not errors can be ignored

        Raises:
            K8sdConnectionError: reraises cluster token revoke failures
        """
        try:
            self.api_manager.remove_node(name)
        except (K8sdConnectionError, InvalidResponseError) as e:
            if ignore_errors or e.code == ErrorCodes.STATUS_NODE_UNAVAILABLE:
                # Let's just ignore some of these expected errors:
                # "Remote end closed connection without response"
                # "Failed to check if node is control-plane"
                # Removing a node that doesn't exist
                log.warning("Remove_Node %s: but with an expected error: %s", name, e)
            else:
                raise


class CosTokenManager:
    """Class for managing COS tokens.

    Attributes:
        allocator_needs_tokens: The allocating node needs a cos-token to join
        strategy: The cos strategy for token creation.
        revoke_on_join: Don't revoke a token once it's joined.
    """

    allocator_needs_tokens: bool = True
    strategy: TokenStrategy = TokenStrategy.COS
    revoke_on_join = False

    def __init__(self, api_manager: K8sdAPIManager):
        """Initialize a CosTokenManager instance.

        Args:
            api_manager (K8sdAPIManager): An K8sdAPIManager object for interacting with k8sd API.
        """
        self.api_manager = api_manager

    def create(self, name: str, token_type: ClusterTokenType) -> SecretStr:
        """Create a COS token.

        Args:
            name (str): The name of the node.
            token_type (ClusterTokenType): The type of cluster token (ignored)

        Returns:
            SecretStr: The created COS token.
        """
        # pylint: disable=unused-argument
        return self.api_manager.request_auth_token(
            username=f"system:cos:{name}", groups=["system:cos"]
        )

    def revoke(self, name: str, ignore_errors: bool):
        """Remove a COS token intentionally left unimplemented.

        Args:
            name (str): The name of the node.
            ignore_errors (bool): Whether or not errors can be ignored
        """


class TokenCollector:
    """Helper class for collecting tokens for units in a relation."""

    def __init__(self, charm: K8sCharm, node_name: str):
        """Initialize a TokenCollector instance.

        Args:
            charm (CharmBase): A charm object representing the current charm.
            node_name (str): current node's name
        """
        self.charm = charm
        self.node_name = node_name

    def request(self, relation: ops.Relation):
        """Ensure this unit is requesting a token.

        Args:
            relation (ops.Relation): The relation on which to request
        """
        # the presence of node-name is used to request a token
        relation.data[self.charm.unit]["node-name"] = self.node_name

    def cluster_name(self, relation: ops.Relation, local: bool) -> str:
        """Get the cluster name from this relation.

        Args:
            relation (ops.Relation): The relation to check
            local (bool):
                True  - Cached through this unit's "joined" field
                        should only be called when certain this unit is clustered
                False - Considers only the connected unit's "cluster-name" field

        Returns:
            the recovered cluster name from existing relations
        """
        cluster_name: Optional[str] = ""
        if not local:
            # recover_cluster_name
            values = set()
            for unit in relation.units:
                if value := relation.data[unit].get("cluster-name"):
                    values |= {value}
            if values:
                assert len(values) == 1, f"Failed to find 1 {relation.name}:cluster-name"  # nosec
                (cluster_name,) = values
        elif not (cluster_name := relation.data[self.charm.unit].get("joined")):
            # joined_cluster_name
            cluster_name = self.cluster_name(relation, False)
            relation.data[self.charm.unit]["joined"] = cluster_name
        return cluster_name or ""

    @contextlib.contextmanager
    def recover_token(self, relation: ops.Relation):
        """Request, recover token, and acknowledge token once used.

        Args:
            relation (ops.Relation): The relation to check

        Yields:
            str: extracted token content
        """
        self.request(relation)

        # Read the secret-id from the relation
        secret_key = SECRET_ID.format(self.charm.unit.name)
        secret_ids = {
            secret_id
            for unit in relation.units | {self.charm.unit}
            if (secret_id := relation.data[unit].get(secret_key))
        }

        assert len(secret_ids) == 1, f"Failed to find 1 {relation.name}:{secret_key}"  # nosec
        (secret_id,) = secret_ids
        assert secret_id, f"{relation.name}:{secret_key} is not valid"  # nosec
        secret = self.charm.model.get_secret(id=secret_id)

        # Get the content from the secret
        content = secret.get_content(refresh=True)
        assert content["token"], f"{relation.name}:token not valid"  # nosec
        yield content["token"]

        # signal that the relation is joined, the token is used
        self.cluster_name(relation, True)


class TokenDistributor:
    """Helper class for distributing tokens to units in a relation."""

    def __init__(self, charm: K8sCharm, node_name: str, api_manager: K8sdAPIManager):
        """Initialize a TokenDistributor instance.

        Args:
            charm (CharmBase): A charm object representing the current charm.
            node_name (str): current node's name
            api_manager: An K8sdAPIManager object for interacting with k8sd API.
        """
        self.charm = charm
        self.node_name = node_name
        self.token_strategies: Dict[TokenStrategy, Union[ClusterTokenManager, CosTokenManager]] = {
            TokenStrategy.CLUSTER: ClusterTokenManager(api_manager),
            TokenStrategy.COS: CosTokenManager(api_manager),
        }

    def _get_juju_secret(self, relation: ops.Relation, unit: ops.Unit) -> Optional[str]:
        """Lookup juju secret offered to a unit on this relation.

        Args:
            relation (ops.Relation): Which relation (cluster or k8s-cluster)
            unit (ops.Unit): The unit the secret is intended for

        Returns:
            secret_id (None | str) if on the relation
        """
        return relation.data[self.charm.unit].get(SECRET_ID.format(unit.name))

    def _revoke_juju_secret(self, relation: ops.Relation, unit: ops.Unit) -> None:
        """Revoke and remove juju secret offered to a unit on this relation.

        Args:
            relation (ops.Relation): Which relation (cluster or k8s-cluster)
            unit (ops.Unit): The unit the secret is intended for
        """
        secret_id = SECRET_ID.format(unit.name)
        if juju_secret := relation.data[self.charm.unit].pop(secret_id, None):
            secret = self.charm.model.get_secret(id=juju_secret)
            secret.remove_all_revisions()

    def active_nodes(self, relation: ops.Relation):
        """Get nodes from application databag for given relation.

        This method filters out entries in the application databag that are not
        to the cluster units. It uses the regex pattern, which matches patterns
        like k8s/0, k8s-worker/0, etc.

        Args:
            relation (ops.Relation): Which relation (cluster or k8s-cluster)

        Returns:
            dict[Unit, str] each unit's node state
        """
        return {
            self.charm.model.get_unit(str(u)): data
            for u, data in relation.data[self.charm.app].items()
            if UNIT_RE.match(u)
        }

    def drop_node(self, relation: ops.Relation, unit: ops.Unit):
        """Remove nodes from application databag for given units.

        Args:
            relation (ops.Relation): Which relation (cluster or k8s-cluster)
            unit (ops.Unit):         Which unit to drop from the application databag
        """
        relation.data[self.charm.app].pop(unit.name, None)

    def update_node(self, relation: ops.Relation, unit: ops.Unit, state: str):
        """Update node within application databag for given units.

        Args:
            relation (ops.Relation): Which relation (cluster or k8s-cluster)
            unit (ops.Unit):         Which unit to update in the application databag
            state (str):             State of joining the cluster
        """
        relation.data[self.charm.app][unit.name] = state

    def allocate_tokens(
        self,
        relation: ops.Relation,
        token_strategy: TokenStrategy,
        token_type: ClusterTokenType = ClusterTokenType.NONE,
    ):
        """Allocate tokens to units in a relation.

        Args:
            relation (ops.Relation): The relation object.
            token_strategy (TokenStrategy): The strategy of token creation.
            token_type (ClusterTokenType): The type of cluster token.
                Defaults to ClusterTokenType.NONE.
        """
        units = relation.units
        if self.charm.app == relation.app:
            # include self in peer relations
            units |= {self.charm.unit}
        assert relation.app, f"Remote application doesn't exist on {relation.name}"  # nosec

        # Select the appropriate token creation strategy
        tokenizer = self.token_strategies.get(token_strategy)
        assert tokenizer, f"Invalid token_strategy: {token_strategy}"  # nosec

        log.info("Allocating %s %s tokens", token_type.name.title(), token_strategy.name.title())
        status.add(
            ops.MaintenanceStatus(
                f"Allocating {token_type.name.title()} {token_strategy.name.title()} tokens"
            )
        )
        local_cluster = self.charm.get_cluster_name()
        relation.data[self.charm.unit]["node-name"] = self.node_name
        relation.data[self.charm.unit]["cluster-name"] = local_cluster
        if not tokenizer.allocator_needs_tokens:
            # the allocator doesn't need a token to join, mark as already joined
            relation.data[self.charm.unit]["joined"] = local_cluster

        for unit in units:
            secret_id = SECRET_ID.format(unit.name)
            remote_cluster = relation.data[unit].get("joined")
            node = relation.data[unit].get("node-name")
            if not node:
                log.info(
                    "Wait for %s token allocation of %s with unit=%s:%s",
                    token_strategy.name.title(),
                    token_type.name.title(),
                    relation.name,
                    unit.name,
                )
                continue  # wait for the joining unit to provide its node-name
            if remote_cluster and remote_cluster != local_cluster:
                # ignore this unit, it's not in our cluster
                log.info(
                    "Ignoring %s token allocation of %s with unit=%s:%s (%s)",
                    token_strategy.name.title(),
                    token_type.name.title(),
                    relation.name,
                    unit.name,
                    node,
                )
                continue  # unit reports it's joined to another cluster
            if remote_cluster == local_cluster:
                # when a unit state it's joined, it accepts
                # ownership of a token. We need to revoke this token
                # if the unit leaves. Let's create a cache in
                # our app's session of this data.
                log.info(
                    "Completed %s token allocation of %s with unit=%s:%s (%s)",
                    token_strategy.name.title(),
                    token_type.name.title(),
                    relation.name,
                    unit.name,
                    node,
                )
                self.update_node(relation, unit, f"joined-{node}")
                if tokenizer.revoke_on_join:
                    self._revoke_juju_secret(relation, unit)

                continue  # unit reports its joined already
            if relation.data[self.charm.unit].get(secret_id):
                # unit already assigned a token
                log.info(
                    "Waiting for %s token to be recovered %s unit=%s:%s (%s)",
                    token_strategy.name.title(),
                    token_type.name.title(),
                    relation.name,
                    unit.name,
                    node,
                )
                continue

            log.info(
                "Creating %s token for %s unit=%s node=%s",
                token_strategy.name.title(),
                token_type.name.title(),
                unit.name,
                node,
            )
            token = tokenizer.create(node, token_type)
            content = {"token": token.get_secret_value()}
            secret = relation.app.add_secret(content)
            secret.grant(relation, unit=unit)
            relation.data[self.charm.unit][secret_id] = secret.id or ""
            self.update_node(relation, unit, f"pending-{node}")

    def revoke_tokens(
        self,
        relation: ops.Relation,
        token_strategy: TokenStrategy,
        token_type: ClusterTokenType,
        to_remove: Optional[ops.Unit] = None,
    ):
        """Revoke tokens from units in a relation.

        Args:
            relation (ops.Relation): The relation object.
            token_strategy (TokenStrategy): The strategy of token creation.
            token_type (ClusterTokenType, optional): The type of cluster token.
                Defaults to ClusterTokenType.NONE.
            to_remove (ops.Unit, optional): unit to ensure its token is revoked
        """
        # any unit currently in the relation
        all_units = relation.units
        if self.charm.app == relation.app:
            # include self in peer relations
            all_units |= {self.charm.unit}

        # any unit in the app_databag, which successfully recovered its token
        app_databag = self.active_nodes(relation)
        joined = set(app_databag.keys())

        # establish the remaining units
        remove = {to_remove} if to_remove else (joined - all_units)
        remaining = joined - remove

        if not remove:
            return

        log.info(
            "%s Token report for %s \n\tjoined=%s\n\tremoving=%s\n\tremaining=%s",
            token_strategy.name.title(),
            relation.name,
            ",".join(sorted(u.name for u in joined)),
            ",".join(sorted(u.name for u in remove)),
            ",".join(sorted(u.name for u in remaining)),
        )

        status.add(
            ops.MaintenanceStatus(
                f"Revoking {token_type.name.title()} {token_strategy.name.title()} tokens"
            )
        )

        for unit in remove:
            if node_state := app_databag.get(unit):
                state, node = node_state.split("-", 1)
                log.info(
                    "Revoking %s, token for %s unit=%s:%s %s node=%s",
                    token_strategy.name.title(),
                    token_type.name.title(),
                    relation.name,
                    unit.name,
                    state,
                    node,
                )
                ignore_errors = self.node_name == node  # removing myself
                ignore_errors |= state == "pending"  # on pending tokens
                # if cluster doesn't match
                ignore_errors |= self.charm.get_cluster_name() != joined_cluster(relation, unit)
                self.token_strategies[token_strategy].revoke(node, ignore_errors)
                self.drop_node(relation, unit)
                self._revoke_juju_secret(relation, unit)


def joined_cluster(relation: ops.Relation, unit: ops.Unit) -> Optional[str]:
    """Get the cluster name from this relation.

    Args:
        relation (ops.Relation): The relation to check
        unit (ops.Unit): The unit to check

    Returns:
        the recovered cluster name from existing relations
    """
    if data := relation.data.get(unit):
        return data.get("joined")
    return None
