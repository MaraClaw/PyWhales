"""Translate validated profiles only through the existing container create API."""

from pywhales.components.container.cli_wrapper import Container
from pywhales.docker_client import DockerClient

from ._policy import CreatePlan, plan_create


def create_from_plan(client: DockerClient, plan: CreatePlan) -> Container:
    """Revalidate then call existing create, including its pull/inspect preflight.

    This can create a real container when passed a real client. It does not start
    it, retry mutations, inspect effective isolation, or implement a secret vault.
    Queue this whole function, not a previously started SDK operation. Revalidation
    is observational: callers must serialize config/environment changes externally.
    """
    checked = plan_create(plan.profile, plan.request, client)
    request = checked.request
    binding = next(
        binding
        for binding in checked.profile.bindings
        if binding.endpoint == request.endpoint
        and binding.config is client.client_config
    )
    binding._prepare(client)
    logging = request.logging
    log_options = [
        f"max-size={logging.max_size_bytes}",
        f"max-file={logging.max_files}",
        f"mode={logging.mode.value}",
    ]
    if logging.buffer_bytes is not None:
        log_options.append(f"max-buffer-size={logging.buffer_bytes}")
    return client.container.create(
        request.image,
        request.command,
        user=request.user,
        cap_add=request.capabilities,
        cap_drop=("ALL",),
        privileged=request.privileged,
        read_only=request.read_only,
        security_options=request.security_options,
        mounts=tuple(
            (
                f"type={mount.kind}",
                f"source={mount.source}",
                f"target={mount.target}",
                f"readonly={'true' if mount.read_only else 'false'}",
            )
            for mount in request.mounts
        ),
        networks=request.networks,
        cpus=request.resources.cpus,
        memory=request.resources.memory_bytes,
        memory_swap=request.resources.memory_bytes,
        pids_limit=request.resources.pids,
        log_driver=logging.driver,
        log_options=log_options,
        runtime=request.runtime,
        pull=request.pull,
        health_start_interval=request.health_start_interval,
    )
