# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Protocol definitions module."""

import ops


class K8sCharmProtocol(ops.CharmBase):
    """Typing for the K8sCharm."""

    def get_cluster_name(self) -> str:
        """Get the cluster name.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    def get_cloud_name(self) -> str:
        """Get the cloud name.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError
