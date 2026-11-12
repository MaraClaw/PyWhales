"""In-memory stand-in for the Docker CLI behind the hydrated-listing tests.

`FakeCli` replaces `subprocess.run` for one resource kind. It answers list and
batch-inspect launches from a mutable resource table, records every launch, and
reports a missing identity the way the real CLI does: found objects on stdout,
the error on stderr, exit code 1. Swarm kinds resolve unique ID prefixes, and
the service listing prints truncated IDs as the real CLI does. No daemon or
real child process is involved.
"""

import json
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from subprocess import CompletedProcess
from typing import Final
from unittest.mock import patch

from pywhales import DockerClient
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException, DockerTimeout, NotASwarmManager

type Snapshot = dict[str, object]

_LIST_VERBS: Final = {
    "container": "list",
    "image": "list",
    "network": "ls",
    "volume": "list",
    "node": "list",
    "service": "list",
    "config": "list",
    "secret": "list",
}
_MISSING_STDERR: Final = {
    "container": "Error: No such container: {}",
    "image": "Error: No such image: {}",
    "network": "Error response from daemon: network {} not found",
    "volume": "Error: No such volume: {}",
    "node": "Error response from daemon: node {} not found",
    "service": "Error response from daemon: service {} not found",
    "config": "Error response from daemon: config {} not found",
    "secret": "Error response from daemon: secret {} not found",
}
_SWARM_KINDS: Final = frozenset({"node", "service", "config", "secret"})
_TRUNCATED_ID_LENGTH: Final = 12
_NOT_A_MANAGER: Final = (
    b"Error response from daemon: This node is not a swarm manager. "
    b'Use "docker swarm init" or "docker swarm join" to connect this node to '
    b"swarm and try again."
)

INSPECT_FAILURES: Final[Mapping[str, type[Exception]]] = {
    "command": DockerException,
    "not-a-manager": NotASwarmManager,
    "timeout": DockerTimeout,
    "malformed": ParsingError,
}
"""Failures `FakeCli.failing` can inject, with the exception each must surface as."""


def identity_of(kind: str, index: int) -> str:
    """Return the full stored identity the CLI would list for `kind`."""
    match kind:
        case "image":
            return f"sha256:{index:064x}"
        case "volume":
            return f"volume-{index}"
        case "node" | "service" | "config" | "secret":
            # 25 characters, the first 12 unique per index like a truncated ID
            return f"{kind[:3]}{index:04d}{'k' * 18}"
        case _:
            return f"{index:064x}"


def snapshot_of(kind: str, identity: str, **overrides: object) -> Snapshot:
    """Return a minimal inspect object for `identity`, with top-level overrides."""
    match kind:
        case "container":
            body: Snapshot = {
                "Id": identity,
                "Name": f"/c-{identity[:6]}",
                "State": {"Running": False, "Status": "exited"},
                "HostConfig": {"AutoRemove": False},
            }
        case "image":
            body = {"Id": identity, "RepoTags": [f"repo:{identity[7:13]}"]}
        case "network":
            body = {"Id": identity, "Name": f"net-{identity[:6]}", "Driver": "bridge"}
        case "volume":
            body = {"Name": identity, "Driver": "local"}
        case "node":
            body = {
                "ID": identity,
                "Description": {"Hostname": f"host-{identity[:7]}"},
                "Spec": {"Role": "worker", "Availability": "active"},
                "Status": {"State": "ready"},
            }
        case "service" | "config" | "secret":
            body = {"ID": identity, "Spec": {"Name": f"{kind[:3]}-{identity[3:7]}"}}
        case unknown:
            raise ValueError(unknown)
    return {**body, **overrides}


def _arguments(command: Sequence[str]) -> tuple[list[str], str | None]:
    """Split a launch's arguments after the verb into targets and a template."""
    targets: list[str] = []
    template: str | None = None
    rest = iter(command[3:])
    for part in rest:
        if part == "--format":
            template = next(rest, None)
        elif not part.startswith("-"):
            targets.append(part)
    return targets, template


class FakeCli:
    """Fake CLI for one resource kind, usable as a `subprocess.run` side effect.

    Attributes:
        launches: Every argv received, in order.
        present: Inspect objects by identity; tests mutate it between launches.
        listing: Lines the list command prints; duplicates are allowed.
        vanish_on_list: Identities deleted as soon as the list command answers.
        vanish_on_resolve: Identities deleted as soon as an `inspect --format`
            launch answers.
        reorder: Return inspect objects in reverse request order when set.
        corrupt: Replacement identity reported for a requested identity.
    """

    def __init__(self, kind: str, listing: Iterable[str]) -> None:
        self.kind = kind
        self.listing = list(listing)
        self.present: dict[str, Snapshot] = {
            identity: snapshot_of(kind, identity) for identity in self.listing
        }
        self.launches: list[list[str]] = []
        self.vanish_on_list: set[str] = set()
        self.vanish_on_resolve: set[str] = set()
        self.reorder = False
        self.corrupt: Mapping[str, str] = {}

    def __call__(
        self, argv: Sequence[object], *_args: object, **_kwargs: object
    ) -> CompletedProcess[bytes]:
        command = [str(part) for part in argv]
        self.launches.append(command)
        verb = command[2] if command[1] == self.kind else command[1]
        targets, template = _arguments(command)
        match verb:
            case "list" | "ls":
                return self._list()
            case "inspect":
                return self._inspect(targets, template)
            case "stop":
                for target in targets:
                    self.running(target, False)
            case "rm":
                return self._remove(targets)
            case _:
                pass
        return CompletedProcess(command, 0, b"", b"")

    def _list(self) -> CompletedProcess[bytes]:
        lines = self.listing
        if self.kind == "service":
            lines = [line[:_TRUNCATED_ID_LENGTH] for line in lines]
        stdout = "\n".join(lines).encode()
        for identity in self.vanish_on_list:
            self.present.pop(identity, None)
        return CompletedProcess([], 0, stdout, b"")

    def _lookup(self, target: str) -> str | None:
        """Return the stored identity `target` names, resolving swarm prefixes."""
        if target in self.present:
            return target
        if self.kind in _SWARM_KINDS:
            matches = [name for name in self.present if name.startswith(target)]
            if len(matches) == 1:
                return matches[0]
        return None

    def _inspect(
        self, targets: Sequence[str], template: str | None
    ) -> CompletedProcess[bytes]:
        looked_up = [(target, self._lookup(target)) for target in targets]
        found = [self.present[name] for _, name in looked_up if name is not None]
        missing = [target for target, name in looked_up if name is None]
        if template is None:
            if self.reorder:
                found.reverse()
            stdout = json.dumps([self._corrupted(item) for item in found]).encode()
        elif template == "{{.ID}}":
            stdout = "\n".join(
                str(self._corrupted(item)[self._id_key]) for item in found
            ).encode()
            for identity in self.vanish_on_resolve:
                self.present.pop(identity, None)
        else:
            raise ValueError(template)
        if missing:
            stderr = "\n".join(
                _MISSING_STDERR[self.kind].format(target) for target in missing
            ).encode()
            return CompletedProcess([], 1, stdout, stderr)
        return CompletedProcess([], 0, stdout, b"")

    @property
    def _id_key(self) -> str:
        match self.kind:
            case "volume":
                return "Name"
            case "node" | "service" | "config" | "secret":
                return "ID"
            case _:
                return "Id"

    def _corrupted(self, item: Snapshot) -> Snapshot:
        key = self._id_key
        replacement = self.corrupt.get(str(item[key]))
        return item if replacement is None else {**item, key: replacement}

    def _remove(self, targets: Sequence[str]) -> CompletedProcess[bytes]:
        for target in targets:
            state = self.present[target].get("State")
            if isinstance(state, dict) and state.get("Running") is True:
                return CompletedProcess(
                    [],
                    1,
                    b"",
                    b"Error response from daemon: cannot remove a running container",
                )
        for target in targets:
            del self.present[target]
        return CompletedProcess([], 0, b"", b"")

    def running(self, identity: str, running: bool) -> None:
        """Set a container's daemon-side `State.Running` flag."""
        self.present[identity]["State"] = {"Running": running}

    def failing(
        self,
        verbs: Iterable[str],
        failure: str | bytes,
        *,
        formatted: bool | None = None,
    ) -> Callable[..., CompletedProcess[bytes]]:
        """Return a `subprocess.run` side effect that fails the chosen verbs.

        Every launch is recorded; launches that are not failed run as usual.

        Args:
            verbs: Subcommands to fail.
            failure: A key of `INSPECT_FAILURES`, or the stderr bytes of a plain
                failed exit.
            formatted: Restrict the failure to inspects that pass `--format`
                (`True`) or do not (`False`); `None` fails both.

        Returns:
            A replacement for the patched `subprocess.run`.
        """
        failed = frozenset(verbs)

        def side_effect(
            argv: Sequence[object], *args: object, **kwargs: object
        ) -> CompletedProcess[bytes]:
            command = [str(part) for part in argv]
            verb = command[2] if command[1] == self.kind else command[1]
            template = _arguments(command)[1]
            if verb not in failed or (
                formatted is not None and (template is not None) != formatted
            ):
                return self(argv, *args, **kwargs)
            self.launches.append(command)
            return self._failure(command, failure)

        return side_effect

    def _failure(
        self, command: list[str], failure: str | bytes
    ) -> CompletedProcess[bytes]:
        match failure:
            case bytes() as stderr:
                return CompletedProcess(command, 1, b"", stderr)
            case "command":
                return CompletedProcess(
                    command, 1, b"", b"Error response from daemon: boom"
                )
            case "not-a-manager":
                return CompletedProcess(command, 1, b"", _NOT_A_MANAGER)
            case "malformed":
                return CompletedProcess(command, 0, b"not json", b"")
            case "timeout":
                # `run` raises this after killing a CLI that outlived its deadline;
                # its partial stderr can look like a missing resource.
                target = (_arguments(command)[0] or ["x"])[0]
                stderr = _MISSING_STDERR[self.kind].format(target).encode()
                raise DockerTimeout(command, -9, b"", stderr)
            case unknown:
                raise ValueError(unknown)

    def inspects(self) -> list[list[str]]:
        """Return the identities of every batch inspect launch, in launch order.

        Launches that only print identities through `--format` are not counted;
        see `resolves`.
        """
        return [
            _arguments(launch)[0]
            for launch in self.launches
            if launch[1:3] == [self.kind, "inspect"] and _arguments(launch)[1] is None
        ]

    def resolves(self) -> list[list[str]]:
        """Return the references of every `inspect --format` launch, in order."""
        return [
            _arguments(launch)[0]
            for launch in self.launches
            if launch[1:3] == [self.kind, "inspect"]
            and _arguments(launch)[1] is not None
        ]

    def lists(self) -> list[list[str]]:
        """Return every list launch argv."""
        verb = _LIST_VERBS[self.kind]
        return [launch for launch in self.launches if launch[1:3] == [self.kind, verb]]


@contextmanager
def fake_daemon(
    cli: FakeCli, *, chunk_size: int = 100, validity: float = 0.0
) -> Generator[DockerClient]:
    """Yield a client whose launches run `cli`, with tunable hydration bounds.

    Args:
        cli: Fake receiving every launch.
        chunk_size: Replacement for the hydration chunk bound.
        validity: Replacement for the snapshot validity period. The default of
            zero expires every unpinned snapshot on its next read, so any
            launch-free read must come from a pinned snapshot.

    Yields:
        A client that launches no real process.
    """
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        patch("subprocess.run", side_effect=cli),
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", validity),
        patch("pywhales.components._hydration.HYDRATION_CHUNK_SIZE", chunk_size),
    ):
        yield client
