# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Token Distributor module."""

import contextlib
import json
import logging
import re
from enum import Enum, auto
from typing import Dict, Generator, Optional

import ops
from literals import (
    CLUSTER_CLUSTER_NAME,
    CLUSTER_JOINED,
    CLUSTER_NODE_NAME,
    CLUSTER_SECRET_ID,
    CLUSTER_TOKEN_FAILURE,
)
from protocols import K8sCharmProtocol
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
)

import charms.contextual_status as status
from charms.contextual_status import ReconcilerError
from charms.k8s.v0.k8sd_api_manager import (
    ErrorCodes,
    InvalidResponseError,
    K8sdAPIManager,
    K8sdConnectionError,
)

log = logging.getLogger(__name__)

UNIT_RE = re.compile(r"^k8s(-worker)?/\d+$")


class TokenFailure(BaseModel):
    """Model for token failure.

    This objects stores information about token failures in a relation.

    The joiner creates one of these when it fails to join the cluster
    and will store it in its unit relation data in 'token-failure' key.
    When the leader sees this failure and the revision matches the one
    in the secret, it will generate a new token and update the secret.

    Attributes:
        revision (int): The revision number of the token provided in the juju-secret
        error (str): The error message associated with the token of the same revision
    """

    revision: int
    error: str


class TokenContent(BaseModel):
    """Model for token data for the relation's secret.

    When serialized, this will be a object with the following fields:
        revision (str): The revision number of the token as a string
        token (str): The token string, expose

        whether serialized to json or to a dict -- always a dict[str,str]

    When deserialized, this will be a object with the following fields:
        revision (int): The revision number of the token as an int
        token (SecretStr): The token string, exposed as a SecretStr for security
    """

    revision: int = Field(default=0)
    token: SecretStr

    def json(self, *args, **kwargs) -> str:
        """Return a JSON representation of the TokenContent."""
        return json.dumps(self.dict(*args, **kwargs))

    def dict(self, *args, **kwargs):
        """Return a dictionary representation of the TokenContent."""
        d = super().dict(*args, **kwargs)
        d["revision"] = str(d["revision"])
        d["token"] = d["token"].get_secret_value()
        return d

    @classmethod
    def load_from_secret(cls, secret: ops.Secret) -> "TokenContent":
        """Load TokenContent from a juju secret."""
        content = secret.get_content(refresh=True)
        return cls.parse_obj(content)


def _get_token_failure(relation: ops.Relation, unit: ops.Unit) -> Optional[TokenFailure]:
    """Get the token failure for a unit on this relation.

    Args:
        relation (ops.Relation): Which relation (cluster or k8s-cluster)
        unit (ops.Unit): The unit the secret is intended for

    Returns:
        TokenFailure: The token failure for the unit, if any
    """
    if token_failure_str := relation.data[unit].get(CLUSTER_TOKEN_FAILURE):
        return TokenFailure.parse_raw(token_failure_str)
    return None


def _set_token_failure(
    relation: ops.Relation, unit: ops.Unit, token_failure: Optional[TokenFailure]
) -> None:
    """Set the token failure for a unit on this relation.

    Args:
        relation (ops.Relation): Which relation (cluster or k8s-cluster)
        unit (ops.Unit): The unit the secret is intended for
        token_failure (TokenFailure): The token failure information
    """
    if token_failure:
        relation.data[unit][CLUSTER_TOKEN_FAILURE] = token_failure.json()
    else:
        relation.data[unit].pop(CLUSTER_TOKEN_FAILURE, None)


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


class TokenManager:
    """Base class for managing tokens.

    Attributes:
        allocator_needs_tokens: Whether the allocating node needs a token to join
        strategy: The token strategy for token creation.
        revoke_on_join: Whether to revoke a token after node joins.
    """

    allocator_needs_tokens: bool
    strategy: TokenStrategy
    revoke_on_join: bool

    def __init__(self, api_manager: K8sdAPIManager):
        """Initialize a ClusterTokenManager instance.

        Args:
            api_manager (K8sdAPIManager): An K8sdAPIManager object for interacting with k8sd API.
        """
        self.api_manager = api_manager

    def create(self, name: str, token_type: ClusterTokenType) -> SecretStr:
        """Create a cluster token."""
        raise NotImplementedError

    def remove(self, name: str, secret: Optional[ops.Secret], ignore_errors: bool):
        """Remove a cluster token."""
        raise NotImplementedError

    def grant(
        self,
        relation: ops.Relation,
        charm: ops.CharmBase,
        unit: ops.Unit,
        secret: ops.Secret,
    ):
        """Share and grant a secret with a unit on this relation.

        Args:
            relation (ops.Relation): Which relation
            charm (ops.CharmBase): A charm object representing the current charm.
            unit (ops.Unit): The unit the secret is intended for
            secret (ops.Secret): The secret to share over the relation
        """
        secret_key = CLUSTER_SECRET_ID.format(unit.name)
        log.info(
            "Grant %s token for '%s' on %s",
            self.strategy.name.title(),
            secret_key,
            relation.name,
        )
        if secret.id is None:
            raise ReconcilerError("Secret ID is None, cannot grant secret")

        relation.data[charm.unit].pop(secret_key, None)
        relation.data[charm.app][secret_key] = secret.id
        secret.grant(relation, unit=unit)

    def revoke(self, relation: ops.Relation, charm: ops.CharmBase, unit: ops.Unit) -> None:
        """Revoke a secret offered to a unit on this relation.

        Args:
            relation (ops.Relation): Which relation
            charm (ops.CharmBase): A charm object representing the current charm.
            unit (ops.Unit): The unit the secret is intended for
        """
        secret_key = CLUSTER_SECRET_ID.format(unit.name)
        log.info(
            "Revoke %s token for '%s' on %s",
            self.strategy.name.title(),
            secret_key,
            relation.name,
        )

        by_app = relation.data[charm.app].pop(secret_key, None)
        by_unit = relation.data[charm.unit].pop(secret_key, None)

        if juju_secret := (by_app or by_unit):
            secret = charm.model.get_secret(id=juju_secret)
            secret.remove_all_revisions()

    def get_juju_secret(
        self, relation: ops.Relation, charm: ops.CharmBase, unit: ops.Unit
    ) -> Optional[ops.Secret]:
        """Lookup juju secret offered to a unit on this relation.

        Args:
            relation (ops.Relation): Which relation (cluster or k8s-cluster)
            charm (ops.CharmBase): A charm object representing the current charm.
            unit (ops.Unit): The unit the secret is intended for

        Returns:
            secret_id (None | ops.Secret) if on the relation
        """
        secret_key = CLUSTER_SECRET_ID.format(unit.name)
        by_app = relation.data[charm.app].get(secret_key)
        by_unit = relation.data[charm.unit].get(secret_key)
        juju_secret = by_app or by_unit

        log.info(
            "%s %s token for '%s' on %s",
            "Found" if juju_secret else "Didn't find",
            self.strategy.name.title(),
            secret_key,
            relation.name,
        )
        if juju_secret:
            return charm.model.get_secret(id=juju_secret)
        return None


class ClusterTokenManager(TokenManager):
    """Class for managing cluster tokens."""

    allocator_needs_tokens: bool = False
    revoke_on_join = True
    strategy: TokenStrategy = TokenStrategy.CLUSTER

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

    def remove(self, name: str, secret: Optional[ops.Secret], ignore_errors: bool):
        """Remove a cluster token.

        Args:
            name (str):                     The name of the node.
            secret (Optional[ops.Secret]):  The secret to remove
            ignore_errors (bool):           Whether or not errors can be ignored

        Raises:
            K8sdConnectionError: reraises cluster token remove failures
        """
        try:
            self.api_manager.remove_node(name)
        except (K8sdConnectionError, InvalidResponseError) as e:
            if ignore_errors or getattr(e, "code") == ErrorCodes.STATUS_NODE_UNAVAILABLE:
                # Let's just ignore some of these expected errors:
                # "Remote end closed connection without response"
                # "Failed to check if node is control-plane"
                # Removing a node that doesn't exist
                log.warning("Remove_Node %s: but with an expected error: %s", name, e)
            else:
                raise


class CosTokenManager(TokenManager):
    """Class for managing COS tokens."""

    allocator_needs_tokens: bool = True
    revoke_on_join = False
    strategy: TokenStrategy = TokenStrategy.COS

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

    def remove(self, name: str, secret: Optional[ops.Secret], ignore_errors: bool):
        """Remove a COS token intentionally left unimplemented.

        Args:
            name (str):                   The name of the node.
            secret (Optional[ops.Secret]): The secret to revoke
            ignore_errors (bool):          Whether or not errors can be ignored

        Raises:
            K8sdConnectionError: reraises cluster token revoke failures
        """
        # pylint: disable=unused-argument
        if not secret:
            return
        content = TokenContent.load_from_secret(secret)
        try:
            self.api_manager.revoke_auth_token(content.token.get_secret_value())
        except (K8sdConnectionError, InvalidResponseError) as e:
            if ignore_errors or getattr(e, "code") == ErrorCodes.STATUS_NODE_UNAVAILABLE:
                # Let's just ignore some of these expected errors:
                # "Remote end closed connection without response"
                # "Failed to check if node is control-plane"
                # Removing a node that doesn't exist
                log.warning("Revoke_Auth_Token %s: but with an expected error: %s", name, e)
            else:
                raise


class TokenCollector:
    """Helper class for collecting tokens for units in a relation."""

    def __init__(self, charm: K8sCharmProtocol, node_name: str):
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
        relation.data[self.charm.unit][CLUSTER_NODE_NAME] = self.node_name

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

        Raises:
            ReconcilerError: If fails to find 1 relation-name:cluster-name.
        """
        cluster_name: Optional[str] = ""
        if not local:
            # recover_cluster_name from a consensus of all units
            values = set()
            for unit in relation.units:
                if value := relation.data[unit].get(CLUSTER_CLUSTER_NAME):
                    values |= {value}
            if values:
                if len(values) != 1:
                    raise ReconcilerError(
                        f"Failed to find 1 {relation.name}:{CLUSTER_CLUSTER_NAME}"
                    )
                (cluster_name,) = values
        elif not (cluster_name := relation.data[self.charm.unit].get(CLUSTER_JOINED)):
            # this unit is not joined
            cluster_name = self.cluster_name(relation, False)
            relation.data[self.charm.unit][CLUSTER_JOINED] = cluster_name
            _set_token_failure(relation, self.charm.unit, None)
        return cluster_name or ""

    @contextlib.contextmanager
    def recover_token(self, relation: ops.Relation) -> Generator[str, None, None]:
        """Request, recover token, and acknowledge token once used.

        Args:
            relation (ops.Relation): The relation to check

        Yields:
            str: extracted token content

        Raises:
            ReconcilerError:
                - If fails to find 1 relation-name:secret-id.
                - If relation-name:secret-key is not valid.
                - If relation-name:token is not valid.
        """
        self.request(relation)

        # Read the secret-id from the relation
        secret_key = CLUSTER_SECRET_ID.format(self.charm.unit.name)
        secret_ids = {
            secret_id
            for unit in relation.units | {self.charm.unit, relation.app}
            if (secret_id := relation.data[unit].get(secret_key))
        }

        if len(secret_ids) != 1:
            raise ReconcilerError(f"Failed to find 1 {relation.name}:{secret_key}")
        (secret_id,) = secret_ids

        try:
            secret = self.charm.model.get_secret(id=secret_id)
        except ops.SecretNotFoundError as e:
            raise ReconcilerError(f"{relation.name}:{secret_key} is deleted") from e

        # Get the content from the secret
        try:
            content = TokenContent.load_from_secret(secret)
        except ValidationError as e:
            raise ReconcilerError(f"{relation.name}:token not valid") from e

        try:
            yield content.token.get_secret_value()
        except Exception as e:
            # Notify the leader that this token failed
            token_failure = TokenFailure(revision=content.revision, error=str(e))
            _set_token_failure(relation, self.charm.unit, token_failure)
            raise e

        # signal that the relation is joined, the token is used
        self.cluster_name(relation, True)


class TokenDistributor:
    """Helper class for distributing tokens to units in a relation."""

    def __init__(self, charm: K8sCharmProtocol, node_name: str, api_manager: K8sdAPIManager):
        """Initialize a TokenDistributor instance.

        Args:
            charm (CharmBase): A charm object representing the current charm.
            node_name (str): current node's name
            api_manager: An K8sdAPIManager object for interacting with k8sd API.
        """
        self.charm = charm
        self.node_name = node_name
        self.token_strategies: Dict[TokenStrategy, TokenManager] = {
            TokenStrategy.CLUSTER: ClusterTokenManager(api_manager),
            TokenStrategy.COS: CosTokenManager(api_manager),
        }

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

    def allocate_tokens(  # noqa: C901
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
            ReconcilerError:
                - If token_strategy is valid.
                - If remote application doesn't exist on relation.
        """
        units = relation.units
        if self.charm.app == relation.app:
            # include self in peer relations
            units |= {self.charm.unit}
        if not relation.app:
            raise ReconcilerError(f"Remote application doesn't exist on {relation.name}")

        # Select the appropriate token creation strategy
        tokenizer = self.token_strategies.get(token_strategy)
        if not tokenizer:
            raise ReconcilerError(f"Invalid token_strategy: {token_strategy}")

        log.info("Allocating %s %s tokens", token_type.name.title(), token_strategy.name.title())
        status.add(
            ops.MaintenanceStatus(
                f"Allocating {token_type.name.title()} {token_strategy.name.title()} tokens"
            )
        )
        local_cluster = self.charm.get_cluster_name()
        relation.data[self.charm.unit][CLUSTER_NODE_NAME] = self.node_name
        relation.data[self.charm.unit][CLUSTER_CLUSTER_NAME] = local_cluster
        if not tokenizer.allocator_needs_tokens:
            # the allocator doesn't need a token to join, mark as already joined
            relation.data[self.charm.unit][CLUSTER_JOINED] = local_cluster

        for unit in units:
            remote_cluster = relation.data[unit].get(CLUSTER_JOINED)
            node = relation.data[unit].get(CLUSTER_NODE_NAME)
            secret = tokenizer.get_juju_secret(relation, self.charm, unit)
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
                    tokenizer.revoke(relation, self.charm, unit)
                elif secret:
                    tokenizer.grant(relation, self.charm, unit, secret)
                continue  # unit reports its joined already
            if secret:
                # unit has been assigned a join-token
                failure = _get_token_failure(relation, unit)
                content = TokenContent.load_from_secret(secret)
                if failure and failure.revision == content.revision:
                    # this token resulted in a failure to join on
                    log.info(
                        "Failure for %s token to join on %s unit=%s:%s (%s)",
                        token_strategy.name.title(),
                        token_type.name.title(),
                        relation.name,
                        unit.name,
                        node,
                    )
                else:
                    log.info(
                        "Waiting for %s token to be recovered %s unit=%s:%s (%s)",
                        token_strategy.name.title(),
                        token_type.name.title(),
                        relation.name,
                        unit.name,
                        node,
                    )
                    tokenizer.grant(relation, self.charm, unit, secret)
                    continue

            log.info(
                "Updating %s token for %s unit=%s node=%s",
                token_strategy.name.title(),
                token_type.name.title(),
                unit.name,
                node,
            )
            token = tokenizer.create(node, token_type)
            if not secret:
                content = TokenContent(token=token, revision=0)
                secret = relation.app.add_secret(content.dict())
            else:
                content = TokenContent.load_from_secret(secret)
                content.token = token
                content.revision += 1
                secret.set_content(content.dict())
            tokenizer.grant(relation, self.charm, unit, secret)
            self.update_node(relation, unit, f"pending-{node}")

    def remove_units(
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
        tokenizer = self.token_strategies.get(token_strategy)
        if not tokenizer:
            raise ReconcilerError(f"Invalid token_strategy: {token_strategy}")

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
                secret = tokenizer.get_juju_secret(relation, self.charm, unit)
                tokenizer.remove(node, secret, ignore_errors)
                self.drop_node(relation, unit)
                tokenizer.revoke(relation, self.charm, unit)


def joined_cluster(relation: ops.Relation, unit: ops.Unit) -> Optional[str]:
    """Get the cluster name from this relation.

    Args:
        relation (ops.Relation): The relation to check
        unit (ops.Unit): The unit to check

    Returns:
        the recovered cluster name from existing relations
    """
    if data := relation.data.get(unit):
        return data.get(CLUSTER_JOINED)
    return None
