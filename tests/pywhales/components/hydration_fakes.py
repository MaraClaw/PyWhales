"""In-memory stand-in for the Docker CLI behind the hydrated-listing tests.

`FakeCli` replaces `subprocess.run` for one resource kind. It answers list and
batch-inspect launches from a mutable resource table, records every launch, and
reports a missing identity the way the real CLI does: found objects on stdout,
the error on stderr, exit code 1. No daemon or real child process is involved.
"""

import json
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from subprocess import CompletedProcess
from typing import Final
from unittest.mock import patch

from pywhales import DockerClient

type Snapshot = dict[str, object]

_LIST_VERBS: Final = {
    "container": "list",
    "image": "list",
    "network": "ls",
    "volume": "list",
}
_MISSING_STDERR: Final = {
    "container": "Error: No such container: {}",
    "image": "Error: No such image: {}",
    "network": "Error response from daemon: network {} not found",
    "volume": "Error: No such volume: {}",
}


def identity_of(kind: str, index: int) -> str:
    """Return the full stored identity the CLI would list for `kind`."""
    match kind:
        case "image":
            return f"sha256:{index:064x}"
        case "volume":
            return f"volume-{index}"
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
        case unknown:
            raise ValueError(unknown)
    return {**body, **overrides}


class FakeCli:
    """Fake CLI for one resource kind, usable as a `subprocess.run` side effect.

    Attributes:
        launches: Every argv received, in order.
        present: Inspect objects by identity; tests mutate it between launches.
        listing: Lines the list command prints; duplicates are allowed.
        vanish_on_list: Identities deleted as soon as the list command answers.
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
        self.reorder = False
        self.corrupt: Mapping[str, str] = {}

    def __call__(
        self, argv: Sequence[object], *_args: object, **_kwargs: object
    ) -> CompletedProcess[bytes]:
        command = [str(part) for part in argv]
        self.launches.append(command)
        verb = command[2] if command[1] == self.kind else command[1]
        targets = [target for target in command[3:] if not target.startswith("-")]
        match verb:
            case "list" | "ls":
                return self._list()
            case "inspect":
                return self._inspect(targets)
            case "stop":
                for target in targets:
                    self.running(target, False)
            case "rm":
                return self._remove(targets)
            case _:
                pass
        return CompletedProcess(command, 0, b"", b"")

    def _list(self) -> CompletedProcess[bytes]:
        stdout = "\n".join(self.listing).encode()
        for identity in self.vanish_on_list:
            self.present.pop(identity, None)
        return CompletedProcess([], 0, stdout, b"")

    def _inspect(self, targets: Sequence[str]) -> CompletedProcess[bytes]:
        found = [self.present[target] for target in targets if target in self.present]
        missing = [target for target in targets if target not in self.present]
        if self.reorder:
            found.reverse()
        found = [self._corrupted(item) for item in found]
        stdout = json.dumps(found).encode()
        if missing:
            stderr = _MISSING_STDERR[self.kind].format(missing[0]).encode()
            return CompletedProcess([], 1, stdout, stderr)
        return CompletedProcess([], 0, stdout, b"")

    def _corrupted(self, item: Snapshot) -> Snapshot:
        key = "Name" if self.kind == "volume" else "Id"
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

    def inspects(self) -> list[list[str]]:
        """Return the identities of every batch inspect launch, in launch order."""
        return [
            launch[3:]
            for launch in self.launches
            if launch[1:3] == [self.kind, "inspect"]
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
