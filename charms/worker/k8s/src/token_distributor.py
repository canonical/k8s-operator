"""Token Distributor module."""

from enum import Enum, auto

import ops
from charms.k8s.v0.k8sd_api_manager import K8sdAPIManager
from ops import CharmBase


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


class TokenDistributor:
    """Helper class for distributing tokens to units in a relation."""

    def __init__(self, api_manager: K8sdAPIManager, charm: CharmBase):
        """Initialize a TokenDistributor instance.

        Args:
            api_manager: An K8sdAPIManager object for interacting with k8sd API.
            charm (CharmBase): A charm object representing the current charm.
        """
        self.api_manager = api_manager
        self.charm = charm
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
        all_units: bool = False,
    ):
        """Distribute tokens to units in a relation.

        Args:
            relation (ops.Relation): The relation object.
            token_strategy (TokenStrategy): The strategy of token creation.
            token_type (ClusterTokenType, optional): The type of cluster token.
                Defaults to ClusterTokenType.NONE.
            all_units (bool, optional): Whether to include all units in the relation.
                Defaults to False.

        Raises:
            ValueError: If an invalid token_strategy is provided.
        """
        units = relation.units
        if all_units:
            units.add(self.charm.unit)

        app_databag: ops.RelationDataContent | dict[str, str] = relation.data.get(
            self.charm.model.app, {}
        )
        sec_key_suffix = (
            "-cluster-secret" if token_strategy == TokenStrategy.CLUSTER else "-cos-secret"
        )

        for unit in units:
            sec_key = f"{unit.name}{sec_key_suffix}"
            if app_databag.get(sec_key):
                continue
            if not (name := relation.data[unit].get("node-name")):
                continue  # wait for the joining unit to provide its node-name

            # Select the appropriate token creation strategy
            token_creator = self.token_creation_strategies.get(token_strategy)
            if not token_creator:
                raise ValueError(f"Invalid token_strategy: {token_strategy}")

            token = token_creator(name, token_type)
            content = {"token": token}
            secret = self.charm.app.add_secret(content)
            secret.grant(relation, unit=unit)
            relation.data[self.charm.app][sec_key] = secret.id or ""
