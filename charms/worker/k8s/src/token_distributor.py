# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Token Distributor module."""

import contextlib
import logging
from enum import Enum, auto
from typing import Set

import charms.contextual_status as status
import ops
from charms.k8s.v0.k8sd_api_manager import K8sdAPIManager, K8sdConnectionError

log = logging.getLogger(__name__)

SECRET_ID = "{0}-secret-id"  # nosec


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


class TokenCollector:
    """Helper class for collecting tokens for units in a relation."""

    def __init__(self, charm: ops.CharmBase, node_name: str):
        """Initialize a TokenCollector instance.

        Args:
            charm (CharmBase): A charm object representing the current charm.
            node_name (str): current node's name
        """
        self.charm = charm
        self.node_name = node_name

    def joined(self, relation: ops.Relation) -> bool:
        """Report if this unit has completed token transfer.

        Args:
            relation (ops.Relation): The relation to check

        Returns:
            bool: if the local unit is joined on this relation.
        """
        data = relation.data[self.charm.unit].get("joined")
        return data == self.node_name

    def request(self, relation: ops.Relation):
        """Ensure this unit is requesting a token.

        Args:
            relation (ops.Relation): The relation on which to request
        """
        # the presence of node-name is used to request a token
        relation.data[self.charm.unit]["node-name"] = self.node_name

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
        relation.data[self.charm.unit]["joined"] = self.node_name


class TokenDistributor:
    """Helper class for distributing tokens to units in a relation."""

    def __init__(self, charm: ops.CharmBase, node_name: str, api_manager: K8sdAPIManager):
        """Initialize a TokenDistributor instance.

        Args:
            charm (CharmBase): A charm object representing the current charm.
            node_name (str): current node's name
            api_manager: An K8sdAPIManager object for interacting with k8sd API.
        """
        self.charm = charm
        self.node_name = node_name
        self.api_manager = api_manager
        self.token_creation_strategies = {
            TokenStrategy.CLUSTER: self._create_cluster_token,
            TokenStrategy.COS: self._create_cos_token,
        }
        self.token_repeaing_strategies = {
            TokenStrategy.CLUSTER: self._revoke_cluster_token,
            TokenStrategy.COS: self._revoke_cos_token,
        }

    def _create_cluster_token(self, name: str, token_type: ClusterTokenType):
        """Create a cluster token.

        Args:
            name (str): The name of the node.
            token_type (ClusterTokenType): The type of cluster token.

        Returns:
            str: The created cluster token.
        """
        worker = token_type == ClusterTokenType.WORKER
        return self.api_manager.create_join_token(name, worker=worker)

    def _create_cos_token(self, name: str, _):
        """Create a COS token.

        Args:
            name (str): The name of the node.

        Returns:
            str: The created COS token.
        """
        return self.api_manager.request_auth_token(
            username=f"system:cos:{name}", groups=["system:cos"]
        )

    def _revoke_cluster_token(self, name: str, _):
        """Remove a cluster token.

        Args:
            name (str): The name of the node.

        Raises:
            K8sdConnectionError: reraises cluster token revoke failures
        """
        try:
            self.api_manager.remove_node(name)
        except K8sdConnectionError:
            if name == self.node_name:
                # I'm unclustering myself
                # Let's just ignore the error
                # "Remote end closed connection without response"
                log.exception("Unclustering onesself can expect an error")
            else:
                raise

    def _revoke_cos_token(self, name: str, _):
        """Remove a COS token.

        Args:
            name (str): The name of the node.
        """
        # TODO: implement removing cos token

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

        Raises:
            ValueError: If an invalid token_strategy is provided.
        """
        invalidate_on_join = token_strategy == TokenStrategy.CLUSTER
        units = relation.units
        if self.charm.app == relation.app:
            # include self in peer relations
            units |= {self.charm.unit}
        assert relation.app, f"Remote application doesn't exist on {relation.name}"  # nosec
        app_databag = relation.data[self.charm.app]

        # Select the appropriate token creation strategy
        token_strat = self.token_creation_strategies.get(token_strategy)
        if not token_strat:
            raise ValueError(f"Invalid token_strategy: {token_strategy}")

        status.add(ops.MaintenanceStatus(f"Allocating {token_type.value} tokens"))
        for unit in units:
            secret_id = SECRET_ID.format(unit.name)
            if not (name := relation.data[unit].get("node-name")):
                log.info(
                    "Wait for node-name of %s unit=%s:%s",
                    token_type.value,
                    relation.name,
                    unit.name,
                )
                continue  # wait for the joining unit to provide its node-name
            if relation.data[unit].get("joined") == name:
                # when a unit state it's joined, it's accepted it took
                # ownership of a token.  We need to revoke this token
                # if the unit leaves. Let's create a cache in the
                # our app's session of this data
                log.info(
                    "Completed token allocation of %s unit=%s:%s",
                    token_type.value,
                    relation.name,
                    unit.name,
                )
                app_databag[unit.name] = name
                if invalidate_on_join:
                    relation.data[self.charm.unit].pop(secret_id, None)
                continue  # unit reports its joined already
            if relation.data[self.charm.unit].get(secret_id):
                continue  # unit already assigned a token

            log.info(
                "Creating token for %s unit=%s hostname=%s", token_type.value, unit.name, name
            )
            token = token_strat(name, token_type)
            content = {"token": token}
            secret = relation.app.add_secret(content)
            secret.grant(relation, unit=unit)
            relation.data[self.charm.unit][secret_id] = secret.id or ""

    def revoke_tokens(
        self,
        relation: ops.Relation,
        token_strategy: TokenStrategy,
        token_type: ClusterTokenType,
        to_remove: Set[ops.Unit] | None = None,
    ):
        """Revoke tokens from units in a relation.

        Args:
            relation (ops.Relation): The relation object.
            token_strategy (TokenStrategy): The strategy of token creation.
            token_type (ClusterTokenType, optional): The type of cluster token.
                Defaults to ClusterTokenType.NONE.
            to_remove (Set[ops.Unit], optional): Specific units to ensure its token is revoked

        Raises:
            ValueError: If an invalid token_strategy is provided.
        """
        app_databag = relation.data[self.charm.app]
        joined = {self.charm.model.get_unit(str(u)) for u in app_databag}
        if to_remove is not None:
            remaining = joined - to_remove
        else:
            remaining = joined & (relation.units | {self.charm.unit})
        remove = joined - remaining

        if not remove:
            return

        log.info(
            "Revoke report for %s \n\tjoined=%s\n\tremoving=%s\n\tremaining=%s",
            relation.name,
            ",".join(sorted(u.name for u in joined)),
            ",".join(sorted(u.name for u in remove)),
            ",".join(sorted(u.name for u in remaining)),
        )

        token_strat = self.token_repeaing_strategies.get(token_strategy)
        if not token_strat:
            raise ValueError(f"Invalid token_strategy: {token_strategy}")

        status.add(ops.MaintenanceStatus(f"Revoking {token_type.value} tokens"))
        for unit in remove:
            if hostname := app_databag.get(unit.name):
                log.info(
                    "Revoking token for %s unit=%s:%s hostname=%s",
                    token_type.value,
                    relation.name,
                    unit.name,
                    hostname,
                )
                token_strat(hostname, token_type)
                del app_databag[unit.name]
