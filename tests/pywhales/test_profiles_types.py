"""Static contracts for profile create choices, checked by ty."""

from typing import Literal, assert_type

from pywhales.admission import EndpointIdentity
from pywhales.profiles import CreateRequest, LogMode, LogPolicy, Mount, ResourceLimits


def _check_closed_create_choices(
    driver: Literal["json-file", "local"],
    kind: Literal["bind", "volume"],
    pull: Literal["never", "missing", "always"],
) -> None:
    """Check that annotated choices are accepted and fields stay narrow."""
    logging = LogPolicy(
        driver=driver,
        max_size_bytes=1_000_000,
        max_files=2,
        mode=LogMode.BLOCKING,
        loss_approved=False,
        buffer_bytes=None,
    )
    mount = Mount(kind=kind, source="/safe", target="/data", read_only=True)
    request = CreateRequest(
        endpoint=EndpointIdentity("test"),
        image="example/app",
        command=(),
        user="1000:1000",
        capabilities=(),
        privileged=False,
        read_only=True,
        security_options=(),
        mounts=(mount,),
        networks=(),
        resources=ResourceLimits(cpus=0.5, memory_bytes=100_000_000, pids=32),
        logging=logging,
        runtime="runc",
        rootless=False,
        pull=pull,
    )

    assert_type(logging.driver, Literal["json-file", "local"])
    assert_type(mount.kind, Literal["bind", "volume"])
    assert_type(request.pull, Literal["never", "missing", "always"])
