"""Plugin-installer shell regressions using local assets and a fake downloader.

Tests run Bash in a temporary sandbox, not Docker or real network downloads.
Installed plugin files are inert fixture bytes rather than executable plugins.
"""

import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

SCRIPTS: Final = Path(__file__).resolve().parents[2] / "scripts"
RELEASES: Final = "https://github.com/docker"
MACHINES: Final = {"x86_64": "amd64", "aarch64": "arm64"}
ASSET_URLS: Final = {
    "compose-5.6.1-amd64": f"{RELEASES}/compose/releases/download/v5.6.1/docker-compose-linux-x86_64",
    "buildx-0.38.1-amd64": f"{RELEASES}/buildx/releases/download/v0.38.1/buildx-v0.38.1.linux-amd64",
    "compose-5.6.1-arm64": f"{RELEASES}/compose/releases/download/v5.6.1/docker-compose-linux-aarch64",
    "buildx-0.38.1-arm64": f"{RELEASES}/buildx/releases/download/v0.38.1/buildx-v0.38.1.linux-arm64",
}
FIXTURE_ASSETS: Final = {key: f"inert {key} fixture\n".encode() for key in ASSET_URLS}
EXISTING: Final = {
    "docker-compose": b"existing compose",
    "docker-buildx": b"existing buildx",
}
ARCHES: Final = pytest.mark.parametrize(
    "machine", list(MACHINES), ids=list(MACHINES.values())
)


@dataclass(frozen=True, slots=True)
class InstallerSandbox:
    """Temporary installer checkout with isolated homes, assets, and plugin files."""

    root: Path

    @property
    def plugins(self) -> Path:
        return self.root / "docker config" / "cli-plugins"

    @property
    def manifest(self) -> Path:
        return self.root / "scripts" / "docker-plugin-checksums.txt"

    @property
    def downloads(self) -> list[str]:
        """Return the URLs the fake wget was asked for, in request order."""
        log = self.root / "downloads"
        return log.read_text().splitlines() if log.exists() else []

    def contents(self) -> dict[str, bytes]:
        """Map every entry of the plugin directory, hidden staging included."""
        return {
            path.name: path.read_bytes() if path.is_file() else b"<directory>"
            for path in self.plugins.iterdir()
        }

    def run(
        self,
        overrides: Mapping[str, str] | None = None,
        *,
        machine: str = "x86_64",
        system: str = "Linux",
    ) -> subprocess.CompletedProcess[str]:
        """Run the copied installer with sandboxed paths and capture its result.

        A fake wget copies local fixture assets and a fake uname reports the
        requested host. The real Bash child has a ten-second timeout; nonzero
        exit status is returned rather than raised.

        Args:
            overrides: Environment values applied after sandbox defaults, or
                None to keep those defaults.
            machine: Machine name the fake ``uname -m`` prints.
            system: Operating system name the fake ``uname -s`` prints.

        Returns:
            The shell result with captured text stdout and stderr.
        """
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"COMPOSE_VERSION", "BUILDX_VERSION", "DOCKER_CONFIG"}
        }
        environment.update(
            HOME=str(self.root / "home"),
            DOCKER_CONFIG=str(self.plugins.parent),
            PATH=f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            FIXTURE_ROOT=str(self.root),
            FIXTURE_MACHINE=machine,
            FIXTURE_SYSTEM=system,
        )
        environment.update(overrides or {})
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "download-docker-plugins.sh")],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


@pytest.fixture
def sandbox(tmp_path: Path) -> InstallerSandbox:
    """Prepare a copied installer, local assets, and inert existing plugin files.

    Rewrite copied checksums for fixture bytes and install a fake wget and uname
    on the sandbox PATH. Pytest owns the temporary tree; preparation starts no
    process.

    Returns:
        A sandbox ready to launch the copied Bash installer without downloading.
    """
    for directory in ("scripts", "bin", "assets", "home"):
        (tmp_path / directory).mkdir()
    for name in ("download-docker-plugins.sh", "docker-plugin-checksums.txt"):
        shutil.copyfile(SCRIPTS / name, tmp_path / "scripts" / name)
    instance = InstallerSandbox(tmp_path)
    pins = []
    for line in instance.manifest.read_text().splitlines():
        if line.startswith("#"):
            continue
        plugin, version, arch, _digest = line.split()
        content = FIXTURE_ASSETS[f"{plugin}-{version}-{arch}"]
        (tmp_path / "assets" / f"{plugin}-{version}-{arch}").write_bytes(content)
        pins.append(
            f"{plugin} {version} {arch} {hashlib.sha256(content).hexdigest()}\n"
        )
    instance.manifest.write_text("".join(pins))
    routes = "\n".join(f"    {url}) asset={key} ;;" for key, url in ASSET_URLS.items())
    transport = tmp_path / "bin" / "wget"
    transport.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
[[ $# == 4 && $1 == -q && $3 == -O ]]
printf '%s\\n' "$2" >> "$FIXTURE_ROOT/downloads"
case "$2" in
{routes}
    *) exit 9 ;;
esac
if [[ "$asset" == "${{FAIL_ASSET-}}" ]]; then
    printf 'partial download' > "$4"
    exit 8
fi
if [[ "$asset" == "${{SIGNAL_ASSET-}}" ]]; then
    printf 'partial download' > "$4"
    kill -"$SIGNAL" "$PPID"
    exit 0
fi
if [[ -n "${{SERVE_ARCH-}}" ]]; then
    asset=${{asset%-*}}-$SERVE_ARCH
fi
cp "$FIXTURE_ROOT/assets/$asset" "$4"
"""
    )
    transport.chmod(0o755)
    host = tmp_path / "bin" / "uname"
    host.write_text(
        """#!/usr/bin/env bash
case "${1-}" in
    -s) printf '%s\\n' "$FIXTURE_SYSTEM" ;;
    -m) printf '%s\\n' "$FIXTURE_MACHINE" ;;
    *) exit 64 ;;
esac
"""
    )
    host.chmod(0o755)
    instance.plugins.mkdir(parents=True)
    for filename, content in EXISTING.items():
        (instance.plugins / filename).write_bytes(content)
    return instance


def test_release_manifest_contains_only_verified_current_pins() -> None:
    pins = [
        line.split()
        for line in (SCRIPTS / "docker-plugin-checksums.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert sorted(pins) == [
        [
            "buildx",
            "0.38.1",
            "amd64",
            "e25d5cd675092bb5d1d03632fdc46cdf3fede7b514dc5f023a105e00d3ce5ad4",
        ],
        [
            "buildx",
            "0.38.1",
            "arm64",
            "3b83b3731916ba433feccaaa689fe5bcdc81c641d92c8e6c2ea865dfa141c97a",
        ],
        [
            "compose",
            "5.6.1",
            "amd64",
            "47737f4236e6d3ccea7291995203f8f018058783b4c3a9469119811a0b5db8c0",
        ],
        [
            "compose",
            "5.6.1",
            "arm64",
            "42cfa75f51ad0ccf290bad37899e0038a8c10a0069e85233ecc1d53e3ff1347d",
        ],
    ]


@ARCHES
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"COMPOSE_VERSION": "5.6.1", "BUILDX_VERSION": "0.38.1"},
    ],
    ids=["defaults", "explicit-current"],
)
def test_activates_both_verified_plugins_for_the_host_architecture(
    sandbox: InstallerSandbox, overrides: Mapping[str, str], machine: str
) -> None:
    arch = MACHINES[machine]

    result = sandbox.run(overrides, machine=machine)

    assert result.returncode == 0, result.stderr
    assert sandbox.downloads == [
        ASSET_URLS[f"compose-5.6.1-{arch}"],
        ASSET_URLS[f"buildx-0.38.1-{arch}"],
    ]
    for plugin, version in (("compose", "5.6.1"), ("buildx", "0.38.1")):
        installed = sandbox.plugins / f"docker-{plugin}"
        assert installed.read_bytes() == FIXTURE_ASSETS[f"{plugin}-{version}-{arch}"]
        assert installed.stat().st_mode & 0o777 == 0o755
    assert sorted(sandbox.contents()) == ["docker-buildx", "docker-compose"]


def test_uses_home_when_docker_config_is_empty(sandbox: InstallerSandbox) -> None:
    result = sandbox.run({"DOCKER_CONFIG": ""})

    assert result.returncode == 0, result.stderr
    plugins = sandbox.root / "home" / ".docker" / "cli-plugins"
    assert (plugins / "docker-compose").read_bytes() == FIXTURE_ASSETS[
        "compose-5.6.1-amd64"
    ]
    assert sandbox.contents() == EXISTING


@pytest.mark.parametrize(
    ("system", "machine"),
    [
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),
        ("Windows_NT", "x86_64"),
        ("Linux", "armv7l"),
        ("Linux", "i686"),
        ("Linux", "riscv64"),
        ("Linux", "ppc64le"),
        ("Linux", "arm64"),
        ("Linux", ""),
    ],
)
def test_unsupported_host_fails_before_download_or_staging(
    sandbox: InstallerSandbox, system: str, machine: str
) -> None:
    result = sandbox.run(system=system, machine=machine)

    assert result.returncode != 0
    assert "Unsupported" in result.stderr
    assert sandbox.downloads == []
    assert sandbox.contents() == EXISTING


@ARCHES
@pytest.mark.parametrize("plugin", ["compose", "buildx"])
def test_digest_mismatch_preserves_both_existing_plugins(
    sandbox: InstallerSandbox, plugin: str, machine: str
) -> None:
    version = "5.6.1" if plugin == "compose" else "0.38.1"
    asset = f"{plugin}-{version}-{MACHINES[machine]}"
    (sandbox.root / "assets" / asset).write_bytes(b"corrupt untrusted bytes")

    result = sandbox.run(machine=machine)

    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert len(sandbox.downloads) == 2
    assert sandbox.contents() == EXISTING


@ARCHES
def test_other_architecture_bytes_are_rejected_for_the_host_pin(
    sandbox: InstallerSandbox, machine: str
) -> None:
    other = "arm64" if MACHINES[machine] == "amd64" else "amd64"

    result = sandbox.run({"SERVE_ARCH": other}, machine=machine)

    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert sandbox.contents() == EXISTING


@ARCHES
def test_download_failure_stops_and_preserves_both_existing_plugins(
    sandbox: InstallerSandbox, machine: str
) -> None:
    result = sandbox.run(
        {"FAIL_ASSET": f"buildx-0.38.1-{MACHINES[machine]}"}, machine=machine
    )

    assert result.returncode == 8
    assert len(sandbox.downloads) == 2
    assert sandbox.contents() == EXISTING


@ARCHES
@pytest.mark.parametrize(
    ("signal_name", "status"), [("TERM", 143), ("INT", 130)], ids=["term", "int"]
)
def test_interruption_removes_staging_and_preserves_both_existing_plugins(
    sandbox: InstallerSandbox, machine: str, signal_name: str, status: int
) -> None:
    result = sandbox.run(
        {
            "SIGNAL_ASSET": f"buildx-0.38.1-{MACHINES[machine]}",
            "SIGNAL": signal_name,
        },
        machine=machine,
    )

    assert result.returncode == status
    assert len(sandbox.downloads) == 2
    assert sandbox.contents() == EXISTING


@ARCHES
@pytest.mark.parametrize("variable", ["COMPOSE_VERSION", "BUILDX_VERSION"])
@pytest.mark.parametrize("version", ["99.99.99", ""])
def test_unknown_version_fails_before_download(
    sandbox: InstallerSandbox, variable: str, version: str, machine: str
) -> None:
    result = sandbox.run({variable: version}, machine=machine)

    assert result.returncode != 0
    assert sandbox.downloads == []
    assert sandbox.contents() == EXISTING


@ARCHES
@pytest.mark.parametrize("plugin", ["compose", "buildx"])
@pytest.mark.parametrize(
    "replacement",
    [
        "",
        "{key} invalid-digest\n",
        "{key} {digest}\n{key} {other}\n",
        "{key} {digest} extra\n",
        "{key} {upper}\n",
        "{key} {short}\n",
    ],
    ids=["missing", "malformed", "duplicate", "extra-field", "uppercase", "short"],
)
def test_missing_or_malformed_selected_pin_fails_before_download(
    sandbox: InstallerSandbox, replacement: str, plugin: str, machine: str
) -> None:
    arch = MACHINES[machine]
    version = "5.6.1" if plugin == "compose" else "0.38.1"
    prefix = f"{plugin} {version} {arch} "
    replaced = replacement.format(
        key=prefix.strip(),
        digest="0" * 64,
        other="1" * 64,
        upper="A" * 64,
        short="0" * 63,
    )
    lines = sandbox.manifest.read_text().splitlines(keepends=True)
    sandbox.manifest.write_text(
        "".join(replaced if line.startswith(prefix) else line for line in lines)
    )

    result = sandbox.run(machine=machine)

    assert result.returncode != 0
    assert sandbox.downloads == []
    assert sandbox.contents() == EXISTING


@ARCHES
def test_pin_for_only_the_other_architecture_does_not_authorize_download(
    sandbox: InstallerSandbox, machine: str
) -> None:
    arch = MACHINES[machine]
    lines = sandbox.manifest.read_text().splitlines(keepends=True)
    sandbox.manifest.write_text(
        "".join(line for line in lines if f" {arch} " not in line)
    )

    result = sandbox.run(machine=machine)

    assert result.returncode != 0
    assert f"No checksum pin for Linux {arch}" in result.stderr
    assert sandbox.downloads == []
    assert sandbox.contents() == EXISTING


@ARCHES
def test_well_formed_incorrect_pin_rejects_downloaded_bytes(
    sandbox: InstallerSandbox, machine: str
) -> None:
    arch = MACHINES[machine]
    compose = hashlib.sha256(FIXTURE_ASSETS[f"compose-5.6.1-{arch}"]).hexdigest()
    sandbox.manifest.write_text(
        f"compose 5.6.1 {arch} {compose}\nbuildx 0.38.1 {arch} {'0' * 64}\n"
    )

    result = sandbox.run(machine=machine)

    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert sandbox.contents() == EXISTING
