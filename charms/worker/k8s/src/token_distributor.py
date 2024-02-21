# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Token Distributor module."""

import contextlib
from enum import Enum, auto

import ops
from charms.k8s.v0.k8sd_api_manager import K8sdAPIManager


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
        secret_key = f"{self.charm.unit.name}-secret-id"
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

    def __init__(self, charm: ops.CharmBase, api_manager: K8sdAPIManager):
        """Initialize a TokenDistributor instance.

        Args:
            charm (CharmBase): A charm object representing the current charm.
            api_manager: An K8sdAPIManager object for interacting with k8sd API.
        """
        self.charm = charm
        self.api_manager = api_manager
        self.token_creation_strategies = {
            TokenStrategy.CLUSTER: self._create_cluster_token,
            TokenStrategy.COS: self._create_cos_token,
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

    def distribute_tokens(
        self,
        relation: ops.Relation,
        token_strategy: TokenStrategy,
        token_type: ClusterTokenType = ClusterTokenType.NONE,
        invalidate_on_join: bool = False,
    ):
        """Distribute tokens to units in a relation.

        Args:
            relation (ops.Relation): The relation object.
            token_strategy (TokenStrategy): The strategy of token creation.
            token_type (ClusterTokenType, optional): The type of cluster token.
                Defaults to ClusterTokenType.NONE.
            invalidate_on_join (bool, optional): Whether to pop the token once remote is joined
                Defaults to False.

        Raises:
            ValueError: If an invalid token_strategy is provided.
        """
        units = relation.units | {self.charm.unit}
        app = relation.app
        assert app, f"Remote application doesn't exist on {relation.name}"  # nosec

        for unit in units:
            secret_id = f"{unit.name}-secret-id"
            if not (name := relation.data[unit].get("node-name")):
                continue  # wait for the joining unit to provide its node-name
            if relation.data[unit].get("joined") == name:
                if invalidate_on_join:
                    relation.data[self.charm.unit].pop(secret_id, None)
                continue  # unit reports its joined already
            if relation.data[self.charm.unit].get(secret_id):
                continue  # unit already assigned a token

            # Select the appropriate token creation strategy
            token_creator = self.token_creation_strategies.get(token_strategy)
            if not token_creator:
                raise ValueError(f"Invalid token_strategy: {token_strategy}")

            token = token_creator(name, token_type)
            content = {"token": token}
            secret = app.add_secret(content)
            secret.grant(relation, unit=unit)
            relation.data[self.charm.unit][secret_id] = secret.id or ""
