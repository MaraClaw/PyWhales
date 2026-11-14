"""Docker CLI commands for Swarm membership, manager state, and join tokens."""

from collections.abc import Iterable
from datetime import timedelta

from pywhales.client_config import Command, DockerCLICaller
from pywhales.utils import ValidPath, run, validate_timeout


class SwarmCLI(DockerCLICaller):
    """Swarm commands that can change daemon membership and expose access tokens."""

    def ca(
        self,
        ca_certificate: ValidPath | None = None,
        ca_key: ValidPath | None = None,
        certificate_expiry: int | timedelta | None = None,
        detach: bool = False,
        external_ca: str | None = None,
        rotate: bool = False,
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Read or rotate the Swarm root certificate authority.

        Args:
            ca_certificate: Path to the PEM-formatted root CA certificate
                to use for the new cluster
            ca_key: Path to the PEM-formatted root CA key to use
                for the new cluster
            certificate_expiry: Node certificate validity in seconds, or None to
                omit the option.
            detach: Exit immediately instead of waiting for the root rotation
                to converge. The function will return `None`.
            external_ca: Specifications of one or more certificate signing endpoints
            rotate: Rotate the swarm CA - if no certificate or key are provided,
                new ones will be generated.
            timeout: Positive finite seconds for each swarm ca launch, replacing the
                client default; None inherits it. The certificate read gets its
                own fresh budget rather than sharing the first launch's.

        Returns:
            None when detach=True. Otherwise, certificate text from a second
            swarm ca invocation after the requested operation completes.

        Raises:
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds.
            DockerTimeout: If a launch exceeds its deadline. It is killed and
                reaped without undoing the rotation, and an expired first launch
                prevents the certificate read.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "swarm", "ca"])

        full_cmd.add_simple_arg("--ca-cert", ca_certificate)
        full_cmd.add_simple_arg("--ca-key", ca_key)
        full_cmd.add_simple_arg(
            "--cert-expiry", stringify_timedelta_for_docker_cli(certificate_expiry)
        )
        full_cmd.add_flag("--detach", detach)
        full_cmd.add_simple_arg("--external-ca", external_ca)
        full_cmd.add_flag("--rotate", rotate)

        run(full_cmd, timeout=timeout)
        if not detach:
            # in "docker swarm ca --rotate", the progress is in stdout, not stderr
            # so we need to run the command a second time to be clean.
            return run([*self.docker_cmd, "swarm", "ca"], timeout=timeout)

    def init(
        self,
        advertise_address: str | None = None,
        autolock: bool = False,
        availability: str = "active",
        data_path_address: str | None = None,
        data_path_port: int | None = None,
        listen_address: str | None = None,
        *,
        default_addr_pools: str | Iterable[str] = (),
        default_addr_pool_mask_length: int | None = None,
        force_new_cluster: bool = False,
        cert_expiry: int | timedelta | None = None,
        dispatcher_heartbeat: int | timedelta | None = None,
        external_ca: str | Iterable[str] = (),
        max_snapshots: int | None = None,
        snapshot_interval: int | None = None,
        task_history_limit: int | None = None,
    ) -> None:
        """Initialize a Swarm.

        If you need the token to join the new swarm from another node,
        use the [`docker.swarm.join_token`](#join_token) function.

        This initializes the selected Docker daemon as a manager and changes its
        cluster state. It requires a running daemon that can initialize a Swarm.

        Args:
            advertise_address: Advertised address (format: `<ip|interface>[:port]`)
            autolock: Enable manager autolocking (requiring an unlock key to start a
                stopped manager)
            availability: Availability of the node ("active"|"pause"|"drain")
            data_path_address: Address or interface to use for data path
                traffic (format is `<ip|interface>`)
            data_path_port: UDP port for data-path traffic, or None to omit it.
            listen_address: address upon which the node listens for inbound
                swarm manager traffic (format: `<ip|interface>[:port]`)
            default_addr_pools: Default address pool in CIDR format. One value or
                an iterable of values, each sent as its own repeated option;
                the default sends none.
            default_addr_pool_mask_length: Subnet mask length of the default
                address pools, or None to omit the option. Zero is sent.
            force_new_cluster: Force creation of a new cluster from the daemon's
                current state.
            cert_expiry: Validity period for node certificates, as seconds or a
                `timedelta`; None omits the option.
            dispatcher_heartbeat: Dispatcher heartbeat period, as seconds or a
                `timedelta`; None omits the option.
            external_ca: Specification of a certificate signing endpoint. One
                value or an iterable of values, each sent as its own repeated
                option; the default sends none.
            max_snapshots: Additional Raft snapshots to retain; None omits the
                option. Zero is sent.
            snapshot_interval: Log entries between Raft snapshots; None omits the
                option. Zero is sent.
            task_history_limit: Task history retention limit; None omits the
                option. Zero is sent.
        """
        full_cmd = Command([*self.docker_cmd, "swarm", "init"])
        full_cmd.add_simple_arg("--advertise-addr", advertise_address)
        full_cmd.add_flag("--autolock", autolock)
        full_cmd.add_simple_arg("--availability", availability)
        full_cmd.add_simple_arg("--data-path-addr", data_path_address)
        full_cmd.add_simple_arg("--data-path-port", data_path_port)
        full_cmd.add_simple_arg("--listen-addr", listen_address)
        full_cmd.add_simple_arg(
            "--cert-expiry", stringify_timedelta_for_docker_cli(cert_expiry)
        )
        full_cmd.add_args_iterable_or_single("--default-addr-pool", default_addr_pools)
        full_cmd.add_simple_arg(
            "--default-addr-pool-mask-length", default_addr_pool_mask_length
        )
        full_cmd.add_simple_arg(
            "--dispatcher-heartbeat",
            stringify_timedelta_for_docker_cli(dispatcher_heartbeat),
        )
        full_cmd.add_args_iterable_or_single("--external-ca", external_ca)
        full_cmd.add_flag("--force-new-cluster", force_new_cluster)
        full_cmd.add_simple_arg("--max-snapshots", max_snapshots)
        full_cmd.add_simple_arg("--snapshot-interval", snapshot_interval)
        full_cmd.add_simple_arg("--task-history-limit", task_history_limit)
        run(full_cmd)

    def join(
        self,
        manager_address: str,
        advertise_address: str | None = None,
        availability: str = "active",
        data_path_address: str | None = None,
        listen_address: str | None = None,
        token: str | None = None,
        *,
        timeout: float | None = None,
    ) -> None:
        """Join the selected Docker daemon to a Swarm.

        Args:
            manager_address: The address of the swarm manager in the format `"{ip}:{port}"`
            advertise_address: Advertised address (format: <ip|interface>[:port])
            availability: Availability of the node
                (`"active"`|`"pause"`|`"drain"`)
            data_path_address: Address or interface to use for data
                path traffic (format: <ip|interface>)
            listen_address: Listen address (format: <ip|interface>[:port])
                (default 0.0.0.0:2377)
            token: Token for entry into the swarm, will determine if
                the node enters the swarm as a manager or a worker.
            timeout: Positive finite seconds from the join CLI's own launch,
                replacing the client default; None inherits it.

        Raises:
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds.
            DockerTimeout: If the CLI exceeds its deadline. It is killed and reaped,
                without undoing the join or guaranteeing daemon-side work stops.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "swarm", "join"])
        full_cmd.add_simple_arg("--advertise-addr", advertise_address)
        full_cmd.add_simple_arg("--availability", availability)
        full_cmd.add_simple_arg("--data-path-addr", data_path_address)
        full_cmd.add_simple_arg("--listen-addr", listen_address)
        full_cmd.add_simple_arg("--token", token)
        full_cmd.append(manager_address)
        run(full_cmd, timeout=timeout)

    def join_token(self, node_type: str, rotate: bool = False) -> str:
        """Return a token for joining the Swarm.

        Join tokens grant access to the cluster; treat the returned text as confidential.

        Args:
            node_type: `"manager"` or `"worker"`
            rotate: Rotate the selected role's token before reading it.

        Returns:
            The selected join token from Docker's quiet output.
        """
        full_cmd = Command([*self.docker_cmd, "swarm", "join-token", "--quiet"])
        full_cmd.add_flag("--rotate", rotate)
        full_cmd.append(node_type)
        return run(full_cmd)

    def leave(self, force: bool = False) -> None:
        """Remove the selected Docker daemon from its Swarm.

        Args:
            force: Force this node to leave the swarm, ignoring warnings
        """
        full_cmd = Command([*self.docker_cmd, "swarm", "leave"])
        full_cmd.add_flag("--force", force)
        run(full_cmd)

    def unlock(self, key: str) -> None:
        """Unlock an autolocked Swarm manager after its daemon restarts.

        The unlock key is sent to the CLI's standard input.

        Args:
            key: The key to unlock the swarm. The key can be obtained on any manager
                with `docker.swarm.unlock_key()`.
        """
        full_cmd = Command([*self.docker_cmd, "swarm", "unlock"])
        run(full_cmd, input=key.encode("utf-8"))

    def unlock_key(self, rotate: bool = False) -> str:
        """Return the key needed to unlock an autolocked Swarm manager.

        Treat the returned key as confidential.

        Args:
            rotate: Rotate the unlock key.

        Returns:
            The unlock key from Docker's quiet output.
        """
        full_cmd = Command([*self.docker_cmd, "swarm", "unlock-key", "--quiet"])
        full_cmd.add_flag("--rotate", rotate)
        return run(full_cmd)

    def update(
        self,
        autolock: bool | None = None,
        cert_expiry: timedelta | None = None,
        dispatcher_heartbeat: timedelta | None = None,
        external_ca: str | None = None,
        max_snapshots: int | None = None,
        snapshot_interval: int | None = None,
        task_history_limit: int | None = None,
    ) -> None:
        """Update the Swarm configuration.

        Args:
            autolock: Enable or disable manager autolocking; None leaves it unchanged.
            cert_expiry: Node certificate validity duration; None leaves it unchanged.
            dispatcher_heartbeat: Dispatcher heartbeat duration; None leaves it unchanged.
            external_ca: Certificate-signing endpoint specification, forwarded
                as `--external-ca`; None omits the option.
            max_snapshots: Additional Raft snapshots to retain; None omits the option.
            snapshot_interval: Log entries between Raft snapshots; None omits the option.
            task_history_limit: Task history retention limit; None omits the option.
        """
        full_cmd = Command([*self.docker_cmd, "swarm", "update"])
        autolock_arg = format_bool_for_cli(autolock)
        if autolock_arg is not None:
            full_cmd.append(f"--autolock={autolock_arg}")
        full_cmd.add_simple_arg(
            "--cert-expiry", stringify_timedelta_for_docker_cli(cert_expiry)
        )
        full_cmd.add_simple_arg(
            "--dispatcher-heartbeat",
            stringify_timedelta_for_docker_cli(dispatcher_heartbeat),
        )
        full_cmd.add_simple_arg("--external-ca", external_ca)
        full_cmd.add_simple_arg("--max-snapshots", max_snapshots)
        full_cmd.add_simple_arg("--snapshot-interval", snapshot_interval)
        full_cmd.add_simple_arg("--task-history-limit", task_history_limit)
        run(full_cmd)


def format_bool_for_cli(flag: bool | None) -> str | None:
    """Return lowercase Boolean text, or None when the option is omitted."""
    if flag is None:
        return
    return str(flag).lower()


def stringify_timedelta_for_docker_cli(
    delta: int | timedelta | None,
) -> str | None:
    """Format a duration in seconds with an s suffix, preserving None as omission."""
    if delta is None:
        return

    if isinstance(delta, timedelta):
        return f"{delta.total_seconds()}s"

    return f"{delta}s"
