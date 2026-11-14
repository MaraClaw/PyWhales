"""Buildx debug adapter protocol commands that run on the caller's stdio.

The debug adapter protocol and the builder connection are not line-delimited,
so these commands inherit stdin, stdout and stderr instead of returning a
process stream. The import of the shared Buildx wrapper happens at call time,
because that module creates the command group defined here.
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from types import MappingProxyType

import pywhales.components.buildx.cli_wrapper
from pywhales.client_config import DockerCLICaller
from pywhales.enums import BuildxCall, BuildxProgress
from pywhales.utils import ValidPath, run, validate_timeout


class BuildxDapCLI(DockerCLICaller):
    """Buildx `dap` command group sharing the client's connection and policy."""

    def build(
        self,
        context_path: ValidPath,
        *,
        add_hosts: Mapping[str, str] = MappingProxyType({}),
        allow: Sequence[str] = (),
        attest: dict[str, str] | None = None,
        build_args: Mapping[str, str] = MappingProxyType({}),
        build_contexts: Mapping[str, str | ValidPath] = MappingProxyType({}),
        builder: pywhales.components.buildx.cli_wrapper.ValidBuilder | None = None,
        cache: bool = True,
        cache_from: str
        | pywhales.components.buildx.cli_wrapper.BuildxOptions
        | builtins.list[pywhales.components.buildx.cli_wrapper.BuildxOptions]
        | None = None,
        cache_to: str
        | pywhales.components.buildx.cli_wrapper.BuildxOptions
        | None = None,
        cgroup_parent: str | None = None,
        file: ValidPath | None = None,
        iidfile: ValidPath | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        load: bool = False,
        metadata_file: ValidPath | None = None,
        network: str | None = None,
        output: pywhales.components.buildx.cli_wrapper.BuildxOptions = MappingProxyType(
            {}
        ),
        platforms: builtins.list[str] | None = None,
        policies: str | Sequence[str] = (),
        progress: BuildxProgress = "auto",
        provenance: bool | Mapping[str, str] | None = None,
        pull: bool = False,
        push: bool = False,
        quiet: bool = False,
        resources: Mapping[str, str | int] = MappingProxyType({}),
        sbom: bool | Mapping[str, str] | None = None,
        secrets: str | Sequence[str] = (),
        shm_size: int | str | None = None,
        ssh: str | None = None,
        tags: str | Sequence[str] = (),
        target: str | None = None,
        ulimits: str | Sequence[str] = (),
        call: BuildxCall | None = None,
        no_cache_filter: str | Sequence[str] | None = None,
        annotations: str | Sequence[str] | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Start a build under the Buildx debug adapter and block until it ends.

        The CLI inherits the caller's stdin, stdout and stderr, so a debug
        client attached to those streams drives the session. The call returns
        when the adapter exits; the build is not recovered as an image and no
        output is captured. Every build option shares the `build()` encoding;
        see `BuildxCLI.build` for their meaning.

        Args:
            context_path: Local or remote build context accepted by Buildx.
            add_hosts: Build-host names mapped to addresses.
            allow: Extra build entitlements; only grant them to trusted builds.
            attest: Attestation parameters, such as `{"type": "sbom"}`.
            build_args: Build-time variable names and values. Eligible values use
                the child environment; strict_env_transport rejects the others
                before lookup.
            build_contexts: Named additional contexts.
            builder: Builder to use; `None` uses the current builder.
            cache: Whether to use the cache.
            cache_from: External cache sources.
            cache_to: External cache destinations.
            cgroup_parent: Parent cgroup for `RUN` instructions.
            file: Local Dockerfile path.
            iidfile: Path where Buildx writes the image ID; `None` omits it.
            labels: Image label names and values.
            load: Request loading with the Docker exporter.
            metadata_file: Path where Buildx writes build metadata.
            network: Network mode for build execution.
            output: Output destination.
            platforms: Target platforms.
            policies: Policy configuration(s), passed as repeated `--policy`.
            progress: Progress rendering mode.
            provenance: Provenance attestation shorthand.
            pull: Always attempt to pull a newer base image.
            push: Request the registry exporter.
            quiet: Suppress build output and have Buildx print the image ID.
            resources: Resource limits for build containers, passed as repeated
                `--resource key=value`.
            sbom: SBOM attestation shorthand.
            secrets: Secret specifications for trusted builds.
            shm_size: Shared memory size for build containers.
            ssh: SSH agent socket or keys to expose to a trusted build.
            tags: Tag or tags to put on the resulting image.
            target: Target build stage.
            ulimits: Ulimit option(s) for build containers.
            call: Frontend method to invoke, such as `"check"` or `"outline"`.
            no_cache_filter: Stage name(s) to rebuild without cache.
            annotations: Image annotation(s).
            debug: Forward Buildx `--debug` immediately after `buildx dap build`.
            timeout: Positive finite seconds from launch, or `None` to inherit the
                client default. On expiry the local CLI is killed and reaped and
                `DockerTimeout` is raised; a process the CLI started may outlive
                it, and daemon-side build work is not cancelled.

        Raises:
            ValueError: `timeout` is Boolean, nonpositive, or nonfinite, or
                strict_env_transport rejects a build argument. Raised before
                any launch.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The CLI exits unsuccessfully.
        """
        validate_timeout(timeout)
        full_cmd, child_env = pywhales.components.buildx.cli_wrapper._build_command(
            self.client_config,
            ("buildx", "dap", "build"),
            add_hosts=add_hosts,
            allow=allow,
            attest=attest,
            build_args=build_args,
            build_contexts=build_contexts,
            builder=builder,
            cache=cache,
            cache_from=cache_from,
            cache_to=cache_to,
            cgroup_parent=cgroup_parent,
            file=file,
            labels=labels,
            load=load,
            metadata_file=metadata_file,
            network=network,
            output=output,
            platforms=platforms,
            policies=policies,
            progress=progress,
            provenance=provenance,
            pull=pull,
            push=push,
            quiet=quiet,
            resources=resources,
            sbom=sbom,
            secrets=secrets,
            shm_size=shm_size,
            ssh=ssh,
            tags=tags,
            target=target,
            ulimits=ulimits,
            call=call,
            no_cache_filter=no_cache_filter,
            annotations=annotations,
            debug=debug,
        )
        full_cmd.add_simple_arg("--iidfile", iidfile)
        full_cmd.append(context_path)
        run(full_cmd, env=child_env, tty=True, timeout=timeout)
