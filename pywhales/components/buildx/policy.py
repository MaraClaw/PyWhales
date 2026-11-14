"""Docker Buildx build-policy commands.

Policies are Rego files that Buildx evaluates next to a Dockerfile. Both commands
launch the configured Docker CLI and its Buildx plugin and wait for it to exit.
"""

from __future__ import annotations

from collections.abc import Iterable

from pywhales.client_config import Command, DockerCLICaller
from pywhales.utils import ValidPath, validate_timeout
from pywhales.utils import run as run_cli


class BuildxPolicyCLI(DockerCLICaller):
    """Policy evaluation and policy tests of `docker buildx policy`."""

    def eval(
        self,
        source: str,
        *,
        file: ValidPath | None = None,
        platform: str | None = None,
        fields: str | Iterable[str] | None = None,
        print: bool = False,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> str:
        """Evaluate the build policy for a source.

        A source the policy allows exits successfully; Buildx writes nothing to
        stdout unless `print=True`. Diagnostics go to stderr, which this call
        does not return.

        Args:
            source: Source to evaluate, such as `docker-image://alpine`.
            file: Policy filename to evaluate, replacing the Buildx default
                `Dockerfile`; `None` omits the option.
            platform: Target platform for the evaluation; `None` omits the
                option.
            fields: Field name or iterable of names to evaluate, passed as
                repeated `--fields` flags. `None` omits the option.
            print: Ask Buildx to print the policy input it evaluated.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`; the log is written to stderr, which this call
                does not return.
            timeout: Positive finite seconds for the local CLI, replacing the
                client default; `None` inherits it. The local CLI is killed and
                reaped on expiry.

        Returns:
            Captured stdout, with one trailing newline removed. It is empty
            unless `print=True`.

        Raises:
            ValueError: `timeout` is not positive and finite. Raised before the
                CLI is looked up.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The Buildx command fails, including when the policy
                denies the source.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "buildx", "policy", "eval"])
        full_cmd.add_flag("--debug", debug)
        full_cmd.add_simple_arg("--builder", builder)
        if fields is not None:
            full_cmd.add_args_iterable_or_single("--fields", fields)
        full_cmd.add_simple_arg("--file", file)
        full_cmd.add_simple_arg("--platform", platform)
        full_cmd.add_flag("--print", print)
        full_cmd.append(source)
        return run_cli(full_cmd, timeout=timeout)

    def test(
        self,
        path: ValidPath,
        *,
        filename: str | None = None,
        run: str | None = None,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> str:
        """Run the policy tests found at a path.

        Buildx resolves `path` against the process working directory and rejects
        absolute paths and a leading `./`; use `.` or a plain relative path.

        Args:
            path: Directory or test file to run, relative to the working
                directory.
            filename: Name of the Dockerfile to validate, replacing the Buildx
                default `Dockerfile`; `None` omits the option.
            run: Run only tests whose name contains this substring; `None` runs
                them all.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`; the log is written to stderr, which this call
                does not return.
            timeout: Positive finite seconds for the local CLI, replacing the
                client default; `None` inherits it. The local CLI is killed and
                reaped on expiry.

        Returns:
            The per-test result report from stdout, with one trailing newline
            removed.

        Raises:
            ValueError: `timeout` is not positive and finite. Raised before the
                CLI is looked up.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The Buildx command fails, including when a test
                fails; the report stays on the exception's `stdout`.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "buildx", "policy", "test"])
        full_cmd.add_flag("--debug", debug)
        full_cmd.add_simple_arg("--builder", builder)
        full_cmd.add_simple_arg("--filename", filename)
        full_cmd.add_simple_arg("--run", run)
        full_cmd.append(path)
        return run_cli(full_cmd, timeout=timeout)
