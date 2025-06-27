# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Configuration for containerd.

The format for the hosts.toml file is as follows:
https://github.com/containerd/containerd/blob/main/docs/hosts.md

The format for the config.toml file is as follows:
https://github.com/containerd/containerd/blob/main/docs/cri/registry.md
"""

import base64
import collections
import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import ops
import pydantic
import tomli_w
import yaml
from literals import CONTAINERD_ARGS

log = logging.getLogger(__name__)


@lru_cache()
def containerd_path() -> Path:
    """Return path to hosts.toml directory.

    Returns:
        path to containerd config

    Raises:
        FileNotFoundError: if containerd config path cannot be found
    """
    for line in CONTAINERD_ARGS.read_text().splitlines():
        if line.startswith("--config="):
            path = line.split("=")[1].strip('"').strip("'")
            return Path(path).parent
    raise FileNotFoundError("Could not find containerd config path in args file.")


def hostsd_path() -> Path:
    """Return path to hosts.toml directory."""
    return containerd_path() / "hosts.d"


def _ensure_file(
    file: Path,
    data: str,
    permissions: Optional[int] = None,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
) -> bool:
    """Ensure file with specific contents, owner:group and permissions exists on disk.

    Args:
        file (Path):       path to the file
        data (str):        content of the file
        permissions (int): permissions on the file
        uid (int):         user owner id
        gid (int):         group owner id

    Returns:
        `True` - if file contents have changed
    """
    file.parent.mkdir(parents=True, exist_ok=True)

    changed = False
    if not file.exists() or file.read_text() != data:
        file.write_text(data)
        changed = True

    if permissions is not None:
        file.chmod(permissions)

    if uid is not None and gid is not None:
        os.chown(file, uid, gid)

    return changed


class Registry(pydantic.BaseModel):
    """Represents a containerd registry.

    Attrs:
        url (HttpUrl):
        host (str):
        username (SecretStr):
        password (SecretStr):
        identitytoken (SecretStr):
        ca_file (str):
        cert_file (str):
        key_file (str):
        skip_verify (bool):
        override_path (bool):
        ca_file_path (Path):
        cert_file_path (Path):
        key_file_path (Path):
        hosts_toml_path (Path):
        auth_config_header (Dict[str, Any]):
        hosts_toml (Dict[str, Any]):
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    # e.g. "https://registry-1.docker.io"
    url: str

    # e.g. "docker.io", or "registry.example.com:32000"
    host: str = ""

    # authentication settings
    username: Optional[pydantic.SecretStr] = None
    password: Optional[pydantic.SecretStr] = None
    identitytoken: Optional[pydantic.SecretStr] = None

    # TLS configuration
    ca_file: Optional[str] = None
    cert_file: Optional[str] = None
    key_file: Optional[str] = None
    skip_verify: Optional[bool] = None

    # misc configuration
    override_path: Optional[bool] = None

    def __init__(self, *args, **kwargs):
        """Create a registry object.

        Args:
            args:   construction positional arguments
            kwargs: construction keyword arguments
        """
        super().__init__(*args, **kwargs)
        url = urlparse(self.url)
        if not self.host and (host := url.netloc):
            self.host = host

    @pydantic.field_validator("url")
    def validate_url(cls, v: str) -> str:
        """Validate the URL.

        Args:
            v (str): value to validate

        Returns:
            validated URL
        """
        pydantic.AnyHttpUrl(v)
        return v

    @pydantic.field_validator("ca_file", "cert_file", "key_file")
    def parse_base64(cls, v: str) -> str:
        """Validate Base64 Content.

        Args:
            v (str): value to validate

        Returns:
            validated content
        """
        return base64.b64decode(v.encode()).decode()

    @property
    def ca_file_path(self) -> Path:
        """Return CA file path.

        Returns:
            path to file
        """
        return hostsd_path() / self.host / "ca.crt"

    @property
    def cert_file_path(self) -> Path:
        """Return Cert file path.

        Returns:
            path to file
        """
        return hostsd_path() / self.host / "client.crt"

    @property
    def key_file_path(self) -> Path:
        """Return Key file path.

        Returns:
            path to file
        """
        return hostsd_path() / self.host / "client.key"

    @property
    def hosts_toml_path(self) -> Path:
        """Return hosts.toml path.

        Returns:
            path to file
        """
        return hostsd_path() / self.host / "hosts.toml"

    @property
    def auth_config_header(self) -> Dict[str, Any]:
        """Return a fixed auth configuration header for registry.

        TODO: May need to be extended for other auth methods (eg. oauth2, etc.)

        Returns:
            This registry's auth content headers
        """
        if self.username and self.password:
            log.debug("Configure basic auth for %s (%s)", self.url, self.host)
            v = self.username.get_secret_value() + ":" + self.password.get_secret_value()
            return {"Authorization": "Basic " + base64.b64encode(v.encode()).decode()}
        if self.identitytoken:
            log.debug("Configure bearer token for %s (%s)", self.url, self.host)
            return {"Authorization": "Bearer " + self.identitytoken.get_secret_value()}
        return {}

    @property
    def hosts_toml(self) -> Dict[str, Any]:
        """Return data for hosts.toml file.

        Returns:
            hosts.toml content
        """
        host_config: Dict[str, Any] = {"capabilities": ["pull", "resolve"]}
        if self.ca_file:
            host_config["ca"] = self.ca_file_path.as_posix()
        if self.cert_file and self.key_file:
            host_config["client"] = [
                [self.cert_file_path.as_posix(), self.key_file_path.as_posix()]
            ]
        elif self.cert_file:
            host_config["client"] = self.cert_file_path.as_posix()

        if self.skip_verify:
            host_config["skip_verify"] = True
        if self.override_path:
            host_config["override_path"] = True
        if config := self.auth_config_header:
            host_config["header"] = config

        return {
            "server": self.url,
            "host": {self.url: host_config},
        }

    def ensure_certificates(self):
        """Ensure client and ca certificates."""
        ca_file_path = self.ca_file_path
        if self.ca_file:
            log.debug("Configure custom CA path %s", ca_file_path)
            _ensure_file(ca_file_path, self.ca_file, 0o600, 0, 0)
        else:
            ca_file_path.unlink(missing_ok=True)

        cert_file_path = self.cert_file_path
        if self.cert_file:
            log.debug("Configure client certificate path %s", cert_file_path)
            _ensure_file(cert_file_path, self.cert_file, 0o600, 0, 0)
        else:
            cert_file_path.unlink(missing_ok=True)

        key_file_path = self.key_file_path
        if self.key_file:
            log.debug("Configure client key path %s", key_file_path)
            _ensure_file(key_file_path, self.key_file, 0o600, 0, 0)
        else:
            key_file_path.unlink(missing_ok=True)

    def ensure_hosts_toml(self):
        """Ensure hosts.toml file."""
        hosts_toml_path = self.hosts_toml_path
        log.debug("Configure hosts.toml %s", hosts_toml_path)
        _ensure_file(hosts_toml_path, tomli_w.dumps(self.hosts_toml), 0o600, 0, 0)


Registries = List[Registry]
registry_list = pydantic.TypeAdapter(Registries)


def parse_registries(yaml_str: str) -> Registries:
    """Parse registry configurations from json string.

    Args:
        yaml_str (str): raw user supplied content

    Returns:
        List[Registries] parsed from yaml_str

    Raises:
        ValueError: if configuration is not valid
    """
    try:
        parsed = yaml.safe_load(yaml_str or "[]")
    except yaml.error.YAMLError as e:
        raise ValueError(f"not valid YAML: {e}") from e

    registries = registry_list.validate_python(parsed)
    dupes = [x for x, y in collections.Counter(x.host for x in registries).items() if y > 1]
    if len(dupes):
        raise ValueError(f"duplicate host definitions: {','.join(dupes)}")
    return registries


def ensure_registry_configs(registries: Registries):
    """Ensure containerd configuration files match the specified registries.

    Args:
        registries (Registries): list of registries
    """
    unneeded = {host.parent.name for host in hostsd_path().glob("**/hosts.toml")}
    for r in registries:
        unneeded -= {r.host}
        log.info("Configure registry %s (%s)", r.host, r.url)
        r.ensure_certificates()
        r.ensure_hosts_toml()

    for h in unneeded:
        log.info("Removing unneeded registry %s", h)
        shutil.rmtree(hostsd_path() / h, ignore_errors=True)


def share(config: str, app: ops.Application, relation: Optional[ops.Relation]):
    """Share containerd configuration over relation application databag.

    Args:
        config (str): list of registries
        app (ops.Application): application to share with.
        relation (ops.Relation): relation on which to share.
    """
    if not relation:
        log.info("No relation to share containerd config.")
        return
    relation.data[app]["custom-registries"] = config


def recover(relation: Optional[ops.Relation]) -> Registries:
    """Share containerd configuration over relation application databag.

    Args:
        relation (ops.Relation): relation on which to receive.

    Returns:
        RegistryConfigs parsed from json_str
    """
    if not relation:
        log.info("No relation to recover containerd config.")
    elif not (app_databag := relation.data.get(relation.app)):
        log.warning("No application data to recover containerd config.")
    elif not (config := app_databag.get("custom-registries")):
        log.warning("No 'custom-registries' to recover containerd config.")
    else:
        log.info("Recovering containerd from relation %s", relation.id)
        return parse_registries(config)
    return []
