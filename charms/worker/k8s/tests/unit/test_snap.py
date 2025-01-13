# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests snap module."""

import gzip
import io
import subprocess
import tarfile
from pathlib import Path
from textwrap import dedent
from unittest import mock

import ops.testing
import pytest
import snap
from charm import K8sCharm


@pytest.fixture(params=["worker", "control-plane"])
def harness(request):
    """Craft a ops test harness.

    Args:
        request: pytest request object
    """
    meta = Path(__file__).parent / "../../charmcraft.yaml"
    if request.param == "worker":
        meta = Path(__file__).parent / "../../../charmcraft.yaml"
    harness = ops.testing.Harness(K8sCharm, meta=meta.read_text())
    harness.begin()
    harness.charm.is_worker = request.param == "worker"
    yield harness
    harness.cleanup()


@pytest.fixture
def missing_snap_installation():
    """Test missing default snap-installation."""
    with mock.patch("snap._default_snap_installation") as mocked:
        mock_path = mocked.return_value
        mock_path.exists.return_value = False
        yield mocked
    mocked.assert_called_once_with()


@pytest.fixture
def snap_installation():
    """Test missing default snap-installation."""
    with mock.patch("snap._default_snap_installation") as mocked:
        mock_path = mocked.return_value
        mock_path.exists.return_value = True
        mock_stream = mock_path.open.return_value.__enter__
        yield mock_stream
    mocked.assert_called_once_with()


@pytest.fixture(autouse=True)
def resource_snap_installation(tmp_path):
    """Add snap-installation resource."""
    with mock.patch("snap._overridden_snap_installation") as mocked:
        mock_path = Path(tmp_path) / "snap_installation.yaml"
        mocked.return_value = mock_path
        yield mock_path


@pytest.fixture()
def block_refresh():
    """Block snap refresh."""
    with mock.patch("snap.block_refresh") as mocked:
        mocked.return_value = False
        yield mocked


@mock.patch("snap.snap_lib.SnapCache")
@pytest.mark.parametrize(
    "state, as_file",
    [
        [("present", "1234", None), False],
        [("present", None, "edge"), False],
        [("present", None, None), True],
    ],
    ids=[
        "installed & store-by-channel",
        "installed & store-by-revision",
        "installed & file-without-override",
    ],
)
def test_block_refresh(cache, state, as_file, caplog, resource_snap_installation):
    """Test block refresh."""
    caplog.set_level(0)
    k8s_snap = cache()["k8s"]
    k8s_snap.state, k8s_snap.revision, k8s_snap.channel = state
    if as_file:
        args = snap.SnapFileArgument(
            name="k8s",
            filename=resource_snap_installation.parent / "k8s_1234.snap",
        )
    else:
        args = snap.SnapStoreArgument(
            name="k8s",
            channel="beta" if k8s_snap.channel else None,
            revision="5678" if k8s_snap.revision else None,
        )
    assert snap.block_refresh(k8s_snap, args)
    assert "Blocking k8s snap refresh" in caplog.text


@mock.patch("snap.snap_lib.SnapCache")
@pytest.mark.parametrize(
    "state, overridden",
    [
        [("available", None, None), None],
        [("present", "1234", None), None],
        [("present", None, "edge"), None],
        [("present", None, None), True],
    ],
    ids=[
        "not installed yet",
        "installed & store-by-same-channel",
        "installed & store-by-same-revision",
        "installed & override",
    ],
)
def test_not_block_refresh(cache, state, overridden, caplog, resource_snap_installation):
    """Test block refresh."""
    caplog.set_level(0)
    k8s_snap = cache()["k8s"]
    k8s_snap.state, k8s_snap.revision, k8s_snap.channel = state
    if overridden:
        resource_snap_installation.write_text(
            "amd64:\n- install-type: store\n  name: k8s\n  channel: edge"
        )
    args = snap.SnapStoreArgument(
        name="k8s",
        channel=k8s_snap.channel,
        revision=k8s_snap.revision,
    )
    assert not snap.block_refresh(k8s_snap, args)
    assert "Allowing k8s snap" in caplog.text


@pytest.mark.usefixtures("missing_snap_installation")
def test_parse_no_file(harness):
    """Test no file exists."""
    with pytest.raises(snap.snap_lib.SnapError):
        snap._parse_management_arguments(harness.charm)


def test_parse_invalid_file(snap_installation, harness):
    """Test file is invalid."""
    snap_installation.return_value = io.BytesIO(b"example: =")
    with pytest.raises(snap.snap_lib.SnapError):
        snap._parse_management_arguments(harness.charm)


@mock.patch("subprocess.check_output")
def test_parse_invalid_arch(mock_checkoutput, snap_installation, harness):
    """Test file has invalid arch."""
    snap_installation.return_value = io.BytesIO(b"{}")
    mock_checkoutput().decode.return_value = "amd64"
    with pytest.raises(snap.snap_lib.SnapError):
        snap._parse_management_arguments(harness.charm)


@mock.patch("subprocess.check_output")
def test_parse_validation_error(mock_checkoutput, snap_installation, harness):
    """Test file cannot be parsed."""
    snap_installation.return_value = io.BytesIO(b"amd64:\n- {}")
    mock_checkoutput().decode.return_value = "amd64"
    with pytest.raises(snap.snap_lib.SnapError):
        snap._parse_management_arguments(harness.charm)


def _create_gzip_tar_string(file_data_dict):
    """Create a gzip-compressed tar archive and return it as a base64-encoded string.

    Args:
        file_data_dict: Dictionary where keys are filenames and values are file content as strings.

    Returns:
        Gzipped tar archive content as a base64-encoded string.
    """
    # Create a BytesIO buffer for the tar file
    tar_buffer = io.BytesIO()

    # Open a tarfile in the buffer
    with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
        for filename, file_content in file_data_dict.items():
            # Create a BytesIO buffer for each file's content
            file_buffer = io.BytesIO(file_content.encode("utf-8"))

            # Create a tarinfo object with details of the file
            tarinfo = tarfile.TarInfo(name=filename)
            tarinfo.size = len(file_content)

            # Add the file content to the tar archive
            tar.addfile(tarinfo, file_buffer)

    # Get the tar content from the buffer
    tar_content = tar_buffer.getvalue()

    # Compress the tar content with gzip
    gzip_buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=gzip_buffer, mode="wb") as gz:
        gz.write(tar_content)

    # Get the gzipped tar content
    return gzip_buffer.getvalue()


@mock.patch("snap.SUPPORT_SNAP_INSTALLATION_OVERRIDE", True)
def test_resource_supplied_installation_by_channel(harness):
    """Test resource installs by store channel."""
    arch = snap._local_arch()
    yaml_data = f"{arch}:\n- install-type: store\n  name: k8s\n  channel: edge"
    file_data = {"./snap_installation.yaml": yaml_data}
    harness.add_resource("snap-installation", _create_gzip_tar_string(file_data))
    args = snap._parse_management_arguments(harness.charm)
    assert len(args) == 1
    assert isinstance(args[0], snap.SnapStoreArgument)
    assert args[0].channel == "edge"
    assert args[0].name == "k8s"
    assert args[0].install_type == "store"


@mock.patch("snap.SUPPORT_SNAP_INSTALLATION_OVERRIDE", True)
def test_resource_supplied_installation_by_filename(harness, resource_snap_installation):
    """Test resource installs by included filename."""
    arch = snap._local_arch()
    yaml_data = dedent(
        f"""
        {arch}:
        - install-type: file
          name: k8s
          filename: ./k8s_xxxx.snap
          dangerous: true
          classic: true
         """
    ).strip()
    file_data = {"./snap_installation.yaml": yaml_data, "./k8s_xxxx.snap": ""}
    harness.add_resource("snap-installation", _create_gzip_tar_string(file_data))
    args = snap._parse_management_arguments(harness.charm)
    assert len(args) == 1
    assert isinstance(args[0], snap.SnapFileArgument)
    assert args[0].install_type == "file"
    assert args[0].name == "k8s"
    assert args[0].filename == resource_snap_installation.parent / "k8s_xxxx.snap"
    assert args[0].dangerous
    assert args[0].classic


@mock.patch("snap.SUPPORT_SNAP_INSTALLATION_OVERRIDE", True)
def test_resource_supplied_snap(harness, resource_snap_installation):
    """Test resource installs by snap only."""
    file_data = {"./k8s_xxxx.snap": ""}
    harness.add_resource("snap-installation", _create_gzip_tar_string(file_data))
    args = snap._parse_management_arguments(harness.charm)
    assert len(args) == 1
    assert isinstance(args[0], snap.SnapFileArgument)
    assert args[0].name == "k8s"
    assert args[0].install_type == "file"
    assert args[0].filename == resource_snap_installation.parent / "k8s_xxxx.snap"
    assert args[0].dangerous
    assert args[0].classic


@mock.patch("subprocess.check_output")
def test_parse_valid_store(mock_checkoutput, snap_installation, harness):
    """Test file parses as store content."""
    content = b"""
amd64:
- install-type: store
  name: k8s
  channel: edge
"""
    snap_installation.return_value = io.BytesIO(content)
    mock_checkoutput().decode.return_value = "amd64"
    args = snap._parse_management_arguments(harness.charm)
    assert args == [
        snap.SnapStoreArgument(name="k8s", channel="edge"),
    ]


@mock.patch("subprocess.check_output")
def test_parse_valid_file(mock_checkoutput, snap_installation, harness):
    """Test file parses as file content."""
    content = b"""
amd64:
- install-type: file
  name: k8s
  filename: path/to/thing
"""
    snap_installation.return_value = io.BytesIO(content)
    mock_checkoutput().decode.return_value = "amd64"
    args = snap._parse_management_arguments(harness.charm)
    assert args == [
        snap.SnapFileArgument(name="k8s", filename=Path("path/to/thing")),
    ]


@pytest.mark.usefixtures("block_refresh")
@mock.patch("snap._parse_management_arguments")
@mock.patch("snap.snap_lib.install_local")
@mock.patch("snap.snap_lib.SnapCache")
def test_management_installs_local(cache, install_local, args, harness):
    """Test installer uses local installer."""
    k8s_snap = cache()["k8s"]
    args.return_value = [snap.SnapFileArgument(name="k8s", filename=Path("path/to/thing"))]
    snap.management(harness.charm)
    k8s_snap.ensure.assert_not_called()
    install_local.assert_called_once_with(filename=Path("path/to/thing"))


@pytest.mark.usefixtures("block_refresh")
@mock.patch("snap._parse_management_arguments")
@mock.patch("snap.snap_lib.install_local")
@mock.patch("snap.snap_lib.SnapCache")
@pytest.mark.parametrize("revision", [None, "123"])
def test_management_installs_store_from_channel(cache, install_local, args, revision, harness):
    """Test installer uses store installer."""
    k8s_snap = cache()["k8s"]
    k8s_snap.revision = revision
    args.return_value = [snap.SnapStoreArgument(name="k8s", channel="edge")]
    snap.management(harness.charm)
    install_local.assert_not_called()
    k8s_snap.ensure.assert_called_once_with(state=snap.snap_lib.SnapState.Present, channel="edge")


@pytest.mark.usefixtures("block_refresh")
@mock.patch("snap._parse_management_arguments")
@mock.patch("snap.snap_lib.install_local")
@mock.patch("snap.snap_lib.SnapCache")
@pytest.mark.parametrize("revision", [None, "456", "123"])
def test_management_installs_store_from_revision(cache, install_local, args, revision, harness):
    """Test installer uses store installer."""
    k8s_snap = cache()["k8s"]
    k8s_snap.revision = revision
    args.return_value = [snap.SnapStoreArgument(name="k8s", revision=123)]
    snap.management(harness.charm)
    install_local.assert_not_called()
    k8s_snap.ensure.assert_called_once_with(state=snap.snap_lib.SnapState.Present, revision="123")


@mock.patch("subprocess.check_output")
def test_version(check_output):
    """Test snap list returns the correct version."""
    check_output.return_value = b""
    assert snap.version(snap="k8s") == (None, False)

    check_output.return_value = """
Name  Version    Rev    Tracking       Publisher   Notes
k8s   1.30.0     1234   latest/stable  canonical✓
""".encode()
    assert snap.version(snap="k8s") == ("1.30.0", False)

    check_output.side_effect = subprocess.CalledProcessError(-1, [], None, None)
    assert snap.version(snap="k8s") == (None, False)
