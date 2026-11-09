"""Docker CLI facade, version metadata, and registry authentication helpers.

Clients share one config across command groups and convenience aliases.
Construction does not probe the executable, daemon, or optional plugins;
operations require their corresponding external CLI support when invoked.
"""

import base64
from collections.abc import Collection, Mapping, Sequence
from typing import Annotated

import pydantic

from pywhales._json import loads_compat
from pywhales.client_config import ClientConfig, Command, DockerCLICaller
from pywhales.components.buildx.cli_wrapper import BuildxCLI
from pywhales.components.compose.cli_wrapper import ComposeCLI
from pywhales.components.config.cli_wrapper import ConfigCLI
from pywhales.components.container.cli_wrapper import ContainerCLI
from pywhales.components.context.cli_wrapper import ContextCLI
from pywhales.components.image.cli_wrapper import ImageCLI
from pywhales.components.manifest.cli_wrapper import ManifestCLI
from pywhales.components.network.cli_wrapper import NetworkCLI
from pywhales.components.node.cli_wrapper import NodeCLI
from pywhales.components.plugin.cli_wrapper import PluginCLI
from pywhales.components.secret.cli_wrapper import SecretCLI
from pywhales.components.service.cli_wrapper import ServiceCLI
from pywhales.components.stack.cli_wrapper import StackCLI
from pywhales.components.swarm.cli_wrapper import SwarmCLI
from pywhales.components.system.cli_wrapper import SystemCLI
from pywhales.components.task.cli_wrapper import TaskCLI
from pywhales.components.trust.cli_wrapper import TrustCLI
from pywhales.components.volume.cli_wrapper import VolumeCLI
from pywhales.enums import ClientTypeInput, ComposeProgressInput
from pywhales.telemetry import TelemetryObserver

from .utils import DockerCamelModel, ValidPath, run


class ClientVersion(DockerCamelModel):
    """Client-side metadata from Docker-compatible version JSON.

    Missing fields remain None. API-version and build-time aliases accommodate
    the alternate spellings accepted by this model.
    """

    platform: dict[str, str] | None = None
    version: str | None = None
    api_version: Annotated[
        str | None,
        pydantic.Field(
            validation_alias=pydantic.AliasChoices("APIVersion", "ApiVersion")
        ),
    ] = None
    default_api_version: str | None = None
    git_commit: str | None = None
    go_version: str | None = None
    os: str | None = None
    arch: str | None = None
    build_time: Annotated[
        str | None,
        pydantic.Field(
            validation_alias=pydantic.AliasChoices("BuildTime", "BuiltTime")
        ),
    ] = None
    context: str | None = None
    experimental: bool | None = None


class ServerVersionComponent(DockerCamelModel):
    """Named daemon component and its optional version details."""

    name: str | None = None
    version: str | None = None
    details: dict[str, str] | None = None


class ServerVersion(DockerCamelModel):
    """Daemon version metadata and optional component descriptions."""

    platform: dict[str, str] | None = None
    components: list[ServerVersionComponent] | None = None
    version: str | None = None
    api_version: Annotated[
        str | None,
        pydantic.Field(
            validation_alias=pydantic.AliasChoices("APIVersion", "ApiVersion")
        ),
    ] = None
    min_api_version: str | None = None
    git_commit: str | None = None
    go_version: str | None = None
    os: str | None = None
    arch: str | None = None
    kernel_version: str | None = None
    build_time: Annotated[
        str | None,
        pydantic.Field(
            validation_alias=pydantic.AliasChoices("BuildTime", "BuiltTime")
        ),
    ] = None


class Version(DockerCamelModel):
    """Combined client and server version response.

    Attributes:
        client: Parsed client metadata, or ``None`` when absent from the response.
        server: Parsed daemon metadata, or ``None`` when absent from the response.
    """

    client: ClientVersion | None = None
    server: ServerVersion | None = None


class DockerClient(DockerCLICaller):
    """Typed facade for Docker-compatible CLI command groups.

    All groups and convenience aliases, such as run, ps, and build, share the
    originating client config. Creating a client does not resolve an executable
    or contact a daemon. Commands may require a running daemon or an installed
    CLI plugin; options are forwarded without version probes or fallback.

    Attributes:
        client_config: Mutable settings shared by command groups and resource
            handles. Execution policies are snapshotted for each new launch.
    """

    def __init__(
        self,
        config: ValidPath | None = None,
        context: str | None = None,
        debug: bool | None = None,
        host: str | None = None,
        log_level: str | None = None,
        tls: bool | None = None,
        tlscacert: ValidPath | None = None,
        tlscert: ValidPath | None = None,
        tlskey: ValidPath | None = None,
        tlsverify: bool | None = None,
        client_config: ClientConfig | None = None,
        compose_files: Sequence[ValidPath] = (),
        compose_profiles: Sequence[str] = (),
        compose_env_files: Sequence[ValidPath] = (),
        compose_project_name: str | None = None,
        compose_project_directory: ValidPath | None = None,
        compose_compatibility: bool | None = None,
        client_call: Sequence[str] = ("docker",),
        client_type: ClientTypeInput = "unknown",
        *,
        compose_dry_run: bool = False,
        compose_parallel: int | None = None,
        compose_progress: ComposeProgressInput | None = None,
        image_stream_buffer_bytes: int | None = None,
        stream_buffer_bytes: int | None = None,
        raw_diagnostics: bool = False,
        telemetry_observer: TelemetryObserver | None = None,
        environment: Mapping[str, str] | None = None,
        default_timeout: float | None = None,
        strict_env_transport: bool = False,
        inherit_environment: bool = True,
        strict_workload_errors: bool = False,
        inherited_environment_names: Collection[str] | None = None,
    ) -> None:
        """Configure command groups and aliases without invoking the CLI.

        When client_config is supplied, retain it unchanged rather than applying
        the other configuration arguments. Otherwise construct a shared config
        from these arguments.

        Args:
            config: Client configuration directory; ``None`` leaves CLI defaults.
            context: Context override passed through ``--context``.
            debug: Emit ``--debug`` only when true; otherwise omit it.
            host: Daemon endpoint passed through ``--host``.
            log_level: CLI logging level; ``None`` leaves the CLI default.
            tls: Emit ``--tls`` only when true; otherwise omit it.
            tlscacert: Trusted CA certificate path; ``None`` omits the option.
            tlscert: Client certificate path; ``None`` omits the option.
            tlskey: Client private-key path; ``None`` omits the option.
            tlsverify: Emit ``--tlsverify`` only when true; otherwise omit it.
            client_config: Existing settings to share instead of creating a new
                config. ``None`` uses the remaining configuration arguments.
            compose_files: Ordered Compose files; empty input leaves selection
                to the CLI.
            compose_profiles: Profiles enabled through repeated Compose options.
            compose_env_files: Ordered environment files; later files override
                earlier values according to Compose semantics.
            compose_project_name: Project-name override for Compose resources.
            compose_project_directory: Compose project-directory override.
            compose_compatibility: Emit Compose compatibility mode only when true.
            client_call: Nonempty executable prefix, optionally including fixed
                arguments. Defaults to ``docker`` and is resolved lazily.
                Alternate clients must support the commands and output consumed.
            client_type: Declared client behavior: ``docker``, ``nerdctl``, or
                ``unknown``. It is not inferred from the executable prefix.
            compose_dry_run: Request Compose previews without applying changes.
                APIs requiring real container handles reject incompatible modes.
            compose_parallel: Compose concurrency limit; ``None`` omits it and
                ``-1`` requests unlimited parallelism.
            compose_progress: Default Compose progress mode; ``None`` leaves
                the CLI default. An explicit build progress option overrides it
                for that call only.
            image_stream_buffer_bytes: Positive pull/push byte-budget override.
                ``None`` uses the general budget at operation time.
            stream_buffer_bytes: Positive byte budget for new owned subprocess
                streams; ``None`` is unbounded. Queued payloads, unfinished
                records, and stderr history have separate limits rather than a
                single total-memory cap.
            raw_diagnostics: Opt this client's automatic diagnostics into
                confidential command, environment, and captured-output values.
                Defaults to safe summaries. Returned data, raw exception
                attributes, uncaptured output, and caller logging are not
                redacted by this policy.
            telemetry_observer: Optional sanitized launch/completion observer.
                Snapshotted by reference at each launch; ordinary callback errors
                are contained. Raw diagnostics never enable payload telemetry.
            environment: Child environment overlay copied into a newly created
                config. It does not affect executable lookup or the parent process.
            default_timeout: Positive finite seconds per local CLI launch, or None
                for no default deadline. Explicit per-call timeouts replace it.
            strict_env_transport: Require container envs and build args to use the
                child environment, rejecting literal argv fallback without exposing
                values. Defaults to false. Image history and provenance are not hidden.
            inherit_environment: Inherit the parent environment by default. When
                false, retain only PATH and HOME, plus USERPROFILE and SYSTEMROOT
                on Windows, before applying the explicit overlay.
            strict_workload_errors: Raise plain DockerException for attached
                workload failures instead of classifying their stderr. Defaults off.
            inherited_environment_names: Copied names selecting exactly which ambient
                variables reach children, overriding inherit_environment. An empty
                collection selects none; None preserves the legacy inheritance mode.
                Explicit client and operation overlays still apply. Executable lookup,
                configuration files and credential helpers are not confined by this.

        Raises:
            ValueError: If a newly created config has an invalid budget, default
                timeout, environment entry, or inherited environment name.
            TypeError: If a newly created config's environment is not a mapping
                of string keys to string values, or inherited_environment_names is
                not a collection of strings (a bare string is rejected).
        """
        if client_config is None:
            client_config = ClientConfig(
                config=config,
                context=context,
                debug=debug,
                host=host,
                log_level=log_level,
                tls=tls,
                tlscacert=tlscacert,
                tlscert=tlscert,
                tlskey=tlskey,
                tlsverify=tlsverify,
                compose_files=list(compose_files),
                compose_profiles=list(compose_profiles),
                compose_env_files=list(compose_env_files),
                compose_project_name=compose_project_name,
                compose_project_directory=compose_project_directory,
                compose_compatibility=compose_compatibility,
                client_call=list(client_call),
                client_type=client_type,
                compose_dry_run=compose_dry_run,
                compose_parallel=compose_parallel,
                compose_progress=compose_progress,
                image_stream_buffer_bytes=image_stream_buffer_bytes,
                stream_buffer_bytes=stream_buffer_bytes,
                raw_diagnostics=raw_diagnostics,
                telemetry_observer=telemetry_observer,
                environment=environment,
                default_timeout=default_timeout,
                strict_env_transport=strict_env_transport,
                inherit_environment=inherit_environment,
                strict_workload_errors=strict_workload_errors,
                inherited_environment_names=inherited_environment_names,
            )
        super().__init__(client_config)

        self.buildx = BuildxCLI(self.client_config)
        self.compose = ComposeCLI(self.client_config)
        self.config = ConfigCLI(self.client_config)
        self.container = ContainerCLI(self.client_config)
        self.context = ContextCLI(self.client_config)
        self.image = ImageCLI(self.client_config)
        self.manifest = ManifestCLI(self.client_config)
        self.network = NetworkCLI(self.client_config)
        self.node = NodeCLI(self.client_config)
        self.plugin = PluginCLI(self.client_config)
        self.secret = SecretCLI(self.client_config)
        self.service = ServiceCLI(self.client_config)
        self.stack = StackCLI(self.client_config)
        self.swarm = SwarmCLI(self.client_config)
        self.system = SystemCLI(self.client_config)
        self.task = TaskCLI(self.client_config)
        self.trust = TrustCLI(self.client_config)
        self.volume = VolumeCLI(self.client_config)

        # aliases
        self.attach = self.container.attach
        self.build = self.buildx.build
        self.commit = self.container.commit
        self.copy = self.container.copy
        self.create = self.container.create
        self.diff = self.container.diff
        self.events = None
        self.execute = self.container.execute
        self.export = self.container.export
        self.images = self.image.list
        self.import_ = self.image.import_
        self.info = self.system.info
        # self.inspect -> too hard to implement
        self.kill = self.container.kill
        self.load = self.image.load
        self.logs = self.container.logs
        self.pause = self.container.pause
        self.ps = self.container.list
        self.pull = self.image.pull
        self.push = self.image.push
        self.rename = self.container.rename
        self.restart = self.container.restart
        self.remove = self.container.remove
        # self.rmi -> doesn't make much sense since it would be docker.remove_image
        self.run = self.container.run
        self.save = self.image.save
        # self.search -> Is anybody going to use it in python?
        self.start = self.container.start
        self.stats = self.container.stats
        self.stop = self.container.stop
        self.tag = self.image.tag
        self.top = self.container.top
        self.unpause = self.container.unpause
        self.update = self.container.update
        self.wait = self.container.wait

    def version(self) -> Version:
        """Fetch client and daemon version metadata through the CLI.

        Block until the command completes. This requires the configured
        executable and whatever daemon access that executable needs.

        Returns:
            Parsed version metadata; client or server fields may be absent.

        Raises:
            ClientNotFoundError: If the configured executable cannot be found.
            DockerException: If the version command fails.
            json.JSONDecodeError: If command output is not valid JSON.
            pydantic.ValidationError: If the JSON fails version-model validation.
        """
        full_cmd = Command([*self.docker_cmd, "version", "-f", "{{json .}}"])
        return Version(**loads_compat(run(full_cmd)))

    def login(
        self,
        server: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """Log in to a Docker registry.

        Block until the CLI finishes and allow it to write directly to the
        terminal. Credentials may be stored by the CLI's credential mechanism.

        Args:
            server: Registry address; ``None`` leaves the CLI's default registry.
            username: Login name; ``None`` omits the username option.
            password: Secret sent through stdin, not argv. ``None`` inherits
                stdin while still requesting ``--password-stdin``.

        Raises:
            ClientNotFoundError: If the configured executable cannot be found.
            DockerException: If the login command fails.
        """
        full_cmd = Command([*self.docker_cmd, "login", "--password-stdin"])

        full_cmd.add_simple_arg("--username", username)
        if server is not None:
            full_cmd.append(server)

        run(
            full_cmd,
            capture_stderr=False,
            capture_stdout=False,
            input=None if password is None else password.encode(),
        )

    def logout(self, server: str | None = None) -> None:
        """Log out of a registry through the CLI's credential mechanism.

        Block until completion and write command output directly to the terminal.

        Args:
            server: Registry address; ``None`` leaves the CLI's default registry.

        Raises:
            ClientNotFoundError: If the configured executable cannot be found.
            DockerException: If the logout command fails.
        """
        full_cmd = Command([*self.docker_cmd, "logout"])

        if server is not None:
            full_cmd.append(server)

        run(full_cmd, capture_stdout=False, capture_stderr=False)

    def login_ecr(
        self,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str | None = None,
        registry: str | None = None,
    ) -> None:
        """Fetch an AWS ECR token and log in using the configured Docker CLI.

        Require the optional botocore dependency and network access to ECR.
        Credential and region arguments pass directly to botocore's client
        creation; omitted values use its normal configuration resolution.
        Use the first authorization response, then perform a blocking login
        whose output is not captured. Botocore service failures propagate.

        Args:
            aws_access_key_id: Explicit AWS access key, or ``None`` for botocore
                credential resolution.
            aws_secret_access_key: Explicit AWS secret, or ``None`` for botocore
                credential resolution.
            region_name: ECR region, or ``None`` for botocore configuration.
            registry: Registry override; ``None`` uses the token response's
                proxy endpoint.

        Raises:
            ModuleNotFoundError: If botocore is not installed.
            DockerException: If the subsequent CLI login fails.
        """
        import botocore.session

        client = botocore.session.get_session().create_client(
            "ecr",
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
        )

        response = client.get_authorization_token()["authorizationData"][0]
        credentials = base64.b64decode(response["authorizationToken"]).decode()
        username, password = credentials.split(":")
        if registry is None:
            registry = str(response["proxyEndpoint"])
        self.login(registry, username, password)
