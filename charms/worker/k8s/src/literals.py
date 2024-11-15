"""Literals for the charm."""

DEPENDENCIES = {
    # NOTE: Update the dependencies for the k8s-charm before releasing.
    "k8s_charm": {
        "dependencies": {"k8s-worker": ">2"},
        "name": "k8s",
        "upgrade_supported": ">=1",
        "version": "2",
    },
    # NOTE: Update the dependencies for the k8s-service before releasing.
    "k8s_service": {
        "dependencies": {"k8s-worker": "^1.31.0"},
        "name": "k8s",
        "upgrade_supported": "^1.30.0",
        "version": "1.31.2",
    },
}
