# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Token Distributor module."""

import contextlib
import logging
from enum import Enum, auto
from typing import Optional

import charms.contextual_status as status
import ops
from charms.k8s.v0.k8sd_api_manager import (
    InvalidResponseError,
    K8sdAPIManager,
    K8sdConnectionError,
)

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
        self.token_revoking_strategies = {
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

    def _revoke_cluster_token(self, name: str, ignore_errors: bool):
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
            if ignore_errors:
                # Let's just ignore some of these expected errors:
                # "Remote end closed connection without response"
                # "Failed to check if node is control-plane"
                log.warning("Remove_Node %s: but with an expected error: %s", name, e)
            else:
                raise

    def _revoke_cos_token(self, name: str, _):
        """Remove a COS token.

        Args:
            name (str): The name of the node.
        """
        # TODO: implement removing cos token

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
        revoke_on_join = token_strategy == TokenStrategy.CLUSTER
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
                # if the unit leaves. Let's create a cache in
                # our app's session of this data.
                log.info(
                    "Completed token allocation of %s unit=%s:%s",
                    token_type.value,
                    relation.name,
                    unit.name,
                )
                app_databag[unit.name] = f"joined-{name}"
                if revoke_on_join:
                    self._revoke_juju_secret(relation, unit)

                continue  # unit reports its joined already
            if relation.data[self.charm.unit].get(secret_id):
                # unit already assigned a token
                log.info(
                    "Waiting for token to be recovered %s unit=%s:%s",
                    token_type.value,
                    relation.name,
                    unit.name,
                )
                continue

            log.info(
                "Creating token for %s unit=%s hostname=%s", token_type.value, unit.name, name
            )
            token = token_strat(name, token_type)
            content = {"token": token.get_secret_value()}
            secret = relation.app.add_secret(content)
            secret.grant(relation, unit=unit)
            relation.data[self.charm.unit][secret_id] = secret.id or ""
            app_databag[unit.name] = f"pending-{name}"

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

        Raises:
            ValueError: If an invalid token_strategy is provided.
        """
        # any unit currently in the relation
        all_units = relation.units
        if self.charm.app == relation.app:
            # include self in peer relations
            all_units |= {self.charm.unit}

        # any unit in the app_databag, which successfully recovered its token
        app_databag = relation.data[self.charm.app]
        joined = {self.charm.model.get_unit(str(u)) for u in app_databag}

        # establish the remaining units
        remove = {to_remove} if to_remove else (joined - all_units)
        remaining = joined - remove

        if not remove:
            return

        log.info(
            "Token report for %s \n\tjoined=%s\n\tremoving=%s\n\tremaining=%s",
            relation.name,
            ",".join(sorted(u.name for u in joined)),
            ",".join(sorted(u.name for u in remove)),
            ",".join(sorted(u.name for u in remaining)),
        )

        token_strat = self.token_revoking_strategies.get(token_strategy)
        if not token_strat:
            raise ValueError(f"Invalid token_strategy: {token_strategy}")

        status.add(ops.MaintenanceStatus(f"Revoking {token_type.value} tokens"))
        for unit in remove:
            if node_state := app_databag.get(unit.name):
                state, node = node_state.split("-", 1)
                log.info(
                    "Revoking token for %s unit=%s:%s %s node=%s",
                    token_type.value,
                    relation.name,
                    unit.name,
                    state,
                    node,
                )
                # ignore pending state, because we can't confirm
                # if the other node ever used the token to join
                # ignore self revocation
                ignore_errors = self.node_name == node or state == "pending"
                token_strat(node, ignore_errors)
                del app_databag[unit.name]
                self._revoke_juju_secret(relation, unit)
