"""Docker Buildx build-record history commands.

Every call launches the configured Docker CLI and its Buildx plugin against the
selected builder. History records belong to that builder's daemon, so removal
and export act on whichever daemon the client is configured for.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from typing import Literal, overload

from pywhales._json import loads_compat
from pywhales.client_config import Command, DockerCLICaller
from pywhales.components._json_output import JsonOutputParser
from pywhales.components.buildx.history_models import BuildRecord, BuildRecordDetail
from pywhales.utils import (
    ProcessStream,
    ValidPath,
    format_filters_for_cli,
    run,
    stream_stdout_and_stderr,
    to_list,
    validate_timeout,
)

type BuildRecordFilters = Mapping[str, str] | Iterable[tuple[str, str]]


def _parse_record(line: str) -> BuildRecord:
    return BuildRecord.model_validate(loads_compat(line))


def _parse_detail(output: str) -> BuildRecordDetail:
    return BuildRecordDetail.model_validate(loads_compat(output))


def _references(refs: str | Iterable[str] | None) -> builtins.list[str]:
    return [] if refs is None else to_list(refs)


def _run_with_debug_text(full_cmd: Command) -> str:
    stdout, stderr = run(full_cmd, return_stderr=True)
    return f"{stdout}\n{stderr}"


class BuildxHistoryCLI(DockerCLICaller):
    """Build-record commands of `docker buildx history`.

    Records are addressed by their bare ID or by a `^N` offset, where `^0` is the
    most recent record. A listing's `BuildRecord.ref` carries a builder prefix;
    use `BuildRecord.id` when passing a listed record to another command.
    """

    def _command(self, *subcommand: str, builder: str | None, debug: bool) -> Command:
        full_cmd = Command([*self.docker_cmd, "buildx", "history", *subcommand])
        full_cmd.add_flag("--debug", debug)
        full_cmd.add_simple_arg("--builder", builder)
        return full_cmd

    @overload
    def list(
        self,
        *,
        filters: BuildRecordFilters | None = None,
        local: bool = False,
        builder: str | None = None,
        debug: Literal[True],
    ) -> str: ...

    @overload
    def list(
        self,
        *,
        filters: BuildRecordFilters | None = None,
        local: bool = False,
        builder: str | None = None,
        debug: Literal[False] = False,
    ) -> builtins.list[BuildRecord]: ...

    @overload
    def list(
        self,
        *,
        filters: BuildRecordFilters | None = None,
        local: bool = False,
        builder: str | None = None,
        debug: bool = False,
    ) -> builtins.list[BuildRecord] | str: ...

    def list(
        self,
        *,
        filters: BuildRecordFilters | None = None,
        local: bool = False,
        builder: str | None = None,
        debug: bool = False,
    ) -> builtins.list[BuildRecord] | str:
        """Return the build records of the selected builder.

        Args:
            filters: Buildx filters such as `{"status": "error"}`, as a mapping or
                as `(key, value)` pairs that keep order and repeated keys. `None`
                lists every record. Invalid filters raise before the CLI is
                looked up.
            local: List only records of the current repository, resolved from the
                process working directory.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`. The call then drops the JSON format and
                returns the human-readable listing, a newline and the captured
                debug log written to stderr.

        Returns:
            Parsed records in the order Buildx lists them, or the combined text
            when `debug=True`.

        Raises:
            TypeError: `filters` is a string or contains an item that is not a
                `(key, value)` tuple of strings.
            ParsingError: A listing line is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
            DockerException: The Buildx command fails.
        """
        filter_args = [] if filters is None else format_filters_for_cli(filters)
        full_cmd = self._command("ls", builder=builder, debug=debug)
        full_cmd.add_args_iterable("--filter", filter_args)
        full_cmd.add_flag("--local", local)
        if debug:
            return _run_with_debug_text(full_cmd)
        full_cmd.extend(["--format", "json"])
        parser = JsonOutputParser(
            "build record listing", self.client_config.raw_diagnostics
        )
        return [
            parser.parse(_parse_record, line)
            for line in run(full_cmd).splitlines()
            if line.strip()
        ]

    @overload
    def inspect(
        self,
        ref: str | None = None,
        *,
        builder: str | None = None,
        debug: Literal[True],
    ) -> str: ...

    @overload
    def inspect(
        self,
        ref: str | None = None,
        *,
        builder: str | None = None,
        debug: Literal[False] = False,
    ) -> BuildRecordDetail: ...

    @overload
    def inspect(
        self,
        ref: str | None = None,
        *,
        builder: str | None = None,
        debug: bool = False,
    ) -> BuildRecordDetail | str: ...

    def inspect(
        self,
        ref: str | None = None,
        *,
        builder: str | None = None,
        debug: bool = False,
    ) -> BuildRecordDetail | str:
        """Return the details of one build record.

        Args:
            ref: Bare record ID or `^N` offset. `None` lets Buildx pick its
                default, the most recent record.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`. The call then drops the JSON format and
                returns the human-readable text, a newline and the captured debug
                log written to stderr.

        Returns:
            The parsed record, or the combined text when `debug=True`.

        Raises:
            ParsingError: The output is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
            DockerException: The Buildx command fails, for example when no record
                matches `ref`.
        """
        full_cmd = self._command("inspect", builder=builder, debug=debug)
        if not debug:
            full_cmd.extend(["--format", "json"])
        if ref is not None:
            full_cmd.append(ref)
        if debug:
            return _run_with_debug_text(full_cmd)
        parser = JsonOutputParser("build record", self.client_config.raw_diagnostics)
        return parser.parse(_parse_detail, run(full_cmd))

    def inspect_attachment(
        self,
        ref: str | None = None,
        digest: str | None = None,
        *,
        type: str | None = None,
        platform: str | None = None,
        builder: str | None = None,
        debug: bool = False,
    ) -> str:
        """Return the content of a build record attachment.

        Args:
            ref: Bare record ID or `^N` offset. `None` lets Buildx pick its
                default, the most recent record.
            digest: Digest of the attachment to read; requires `ref`. `None`
                selects by `type` and `platform`, or Buildx's default.
            type: Attachment media type to select, such as
                `https://slsa.dev/provenance/v1`.
            platform: Platform of the attachment to select.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`; the log is written to stderr, which this
                call does not return.

        Returns:
            The attachment content as text, with one trailing newline removed.

        Raises:
            ValueError: `digest` is given without `ref`. Raised before the CLI is
                looked up.
            DockerException: The Buildx command fails.
        """
        if digest is not None and ref is None:
            raise ValueError("digest requires ref")
        full_cmd = self._command("inspect", "attachment", builder=builder, debug=debug)
        full_cmd.add_simple_arg("--type", type)
        full_cmd.add_simple_arg("--platform", platform)
        if ref is not None:
            full_cmd.append(ref)
        if digest is not None:
            full_cmd.append(digest)
        return run(full_cmd)

    def logs(
        self,
        ref: str | None = None,
        *,
        progress: Literal["plain", "rawjson", "tty"] | None = None,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> str:
        """Return the logs of a build record.

        Buildx writes these logs to stderr, so the returned text is the captured
        stderr stream.

        Args:
            ref: Bare record ID or `^N` offset. `None` lets Buildx pick its
                default, the most recent record.
            progress: Progress rendering of the replayed log; `None` leaves the
                Buildx default, `plain`.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`; its log shares stderr with the build log and
                is part of the returned text.
            timeout: Positive finite seconds for the local CLI, replacing the
                client default; `None` inherits it. The local CLI is killed and
                reaped on expiry.

        Returns:
            The log text, with one trailing newline removed.

        Raises:
            ValueError: `timeout` is not positive and finite. Raised before the
                CLI is looked up.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The Buildx command fails.
        """
        validate_timeout(timeout)
        full_cmd = self._command("logs", builder=builder, debug=debug)
        full_cmd.add_simple_arg("--progress", progress)
        if ref is not None:
            full_cmd.append(ref)
        _, stderr = run(full_cmd, return_stderr=True, timeout=timeout)
        return stderr

    def export(
        self,
        refs: str | Iterable[str] | None = None,
        *,
        all: bool = False,
        finalize: bool = False,
        output: ValidPath,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Export build records into a Docker Desktop bundle file.

        Args:
            refs: Bare record ID, `^N` offset, or an iterable of them. `None`
                lets Buildx pick its default selection.
            all: Export every record of the builder; cannot be combined with
                `refs`.
            finalize: Ask Buildx to finalize records before exporting them.
            output: File path that receives the bundle.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`; the log is written to stderr, which this call
                does not return.
            timeout: Positive finite seconds for the local CLI, replacing the
                client default; `None` inherits it. The local CLI is killed and
                reaped on expiry.

        Raises:
            ValueError: `timeout` is not positive and finite, or `all` is combined
                with `refs`. Raised before the CLI is looked up.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The Buildx command fails.
        """
        validate_timeout(timeout)
        references = _references(refs)
        if all and references:
            raise ValueError("all cannot be combined with refs")
        full_cmd = self._command("export", builder=builder, debug=debug)
        full_cmd.add_flag("--all", all)
        full_cmd.add_flag("--finalize", finalize)
        full_cmd.add_simple_arg("--output", output)
        full_cmd += references
        run(full_cmd, timeout=timeout)

    def import_(
        self,
        files: ValidPath | Iterable[ValidPath],
        *,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> builtins.list[str]:
        """Import build records from bundle files into Docker Desktop.

        Imported records land in Docker Desktop's `.imported` store, not in a
        builder's history, so `list()` and `open()` don't see them. Buildx also
        tries to open the first record in the default browser.

        Args:
            files: Bundle file path or an iterable of paths; must not be empty.
            builder: Builder name, forwarded as `--builder`; Buildx v0.39.0
                accepts it but uploads to Docker Desktop regardless.
            debug: Pass `--debug`; the debug log lines on stderr are discarded.
            timeout: Positive finite seconds for the local CLI, replacing the
                client default; `None` inherits it. The local CLI is killed and
                reaped on expiry.

        Returns:
            One `docker-desktop://` dashboard URL per imported record, in the
            order Buildx printed them on stderr.

        Raises:
            ValueError: `timeout` is not positive and finite, or `files` is empty.
                Raised before the CLI is looked up.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The Buildx command fails. If only the browser launch
                fails, the upload has already succeeded and the URLs are in the
                exception's `stderr`.
        """
        validate_timeout(timeout)
        paths = to_list(files)
        if not paths:
            raise ValueError("files must not be empty")
        full_cmd = self._command("import", builder=builder, debug=debug)
        full_cmd.add_args_iterable("--file", paths)
        _, stderr = run(full_cmd, return_stderr=True, timeout=timeout)
        return [
            line.strip()
            for line in stderr.splitlines()
            if line.strip().startswith("docker-desktop://")
        ]

    def open(
        self,
        ref: str | None = None,
        *,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Open a build record in Docker Desktop.

        Buildx resolves the record in the builder's history (imported records
        aren't found) and opens its `docker-desktop://` URL in the default
        browser. The call waits for the local CLI to exit and prints nothing.

        Args:
            ref: Bare record ID or `^N` offset. `None` lets Buildx pick its
                default, the most recent record.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`; the log is written to stderr, which this call
                does not return.
            timeout: Positive finite seconds for the local CLI, replacing the
                client default; `None` inherits it. The local CLI is killed and
                reaped on expiry.

        Raises:
            ValueError: `timeout` is not positive and finite. Raised before the
                CLI is looked up.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The Buildx command fails: no matching record, or
                no handler can open the `docker-desktop://` URL.
        """
        validate_timeout(timeout)
        full_cmd = self._command("open", builder=builder, debug=debug)
        if ref is not None:
            full_cmd.append(ref)
        run(full_cmd, timeout=timeout)

    def remove(
        self,
        refs: str | Iterable[str] | None = None,
        *,
        all: bool = False,
        builder: str | None = None,
        debug: bool = False,
    ) -> None:
        """Remove build records from the builder's history.

        Removal is permanent. Pass the records explicitly: with no `refs` and
        `all=False` the call forwards an empty selection and Buildx applies its
        own default.

        Args:
            refs: Bare record ID, `^N` offset, or an iterable of them.
            all: Remove every record of the builder; cannot be combined with
                `refs`.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`; the log is written to stderr, which this call
                does not return.

        Raises:
            ValueError: `all` is combined with `refs`. Raised before the CLI is
                looked up.
            DockerException: The Buildx command fails.
        """
        references = _references(refs)
        if all and references:
            raise ValueError("all cannot be combined with refs")
        full_cmd = self._command("rm", builder=builder, debug=debug)
        full_cmd.add_flag("--all", all)
        full_cmd += references
        run(full_cmd)

    def trace(
        self,
        ref: str | None = None,
        *,
        addr: str | None = None,
        compare: str | None = None,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> ProcessStream:
        """Start the OpenTelemetry trace command and return its owned stream.

        The CLI starts during the call. Buildx behaves differently by mode, and
        this wrapper captures output through pipes, not a terminal:

        - Without `compare`, Buildx writes the trace as Jaeger JSON to stdout and
          exits. The stream yields those lines and ends; `addr` has no effect.
        - With `compare`, Buildx starts its trace UI server, writes a
          `Trace available at http://...` line to stderr, and runs until it is
          stopped.

        Closing the stream kills only the local `docker` process, not the
        `docker-buildx` plugin it launched. Live checks on macOS with Docker CLI
        29.9.3 and Buildx v0.39.0 found that plugin still serving after `close()`
        in `compare` mode. Stop that server yourself, or avoid `compare`.

        Args:
            ref: Bare record ID or `^N` offset. `None` lets Buildx pick its
                default, the most recent record.
            addr: Address for the UI server; `None` keeps the Buildx default,
                `127.0.0.1:0`.
            compare: Bare ID of a second record to compare against `ref`.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`.
            timeout: Positive finite seconds from launch, replacing the client
                default; `None` inherits it. The deadline keeps running while the
                stream is not consumed.

        Returns:
            An owned stream of `(source, payload)` pairs labelled `stdout` or
            `stderr`, one line per payload. Exhaust it, or use `close()` or a
            context manager on early exit.

        Raises:
            ValueError: `timeout` is not positive and finite. Raised before the
                CLI is looked up.
            DockerTimeout: Delivered by the stream when the local CLI exceeds its
                deadline.
            DockerException: Delivered by the stream when the CLI exits
                unsuccessfully.
        """
        validate_timeout(timeout)
        full_cmd = self._command("trace", builder=builder, debug=debug)
        full_cmd.add_simple_arg("--addr", addr)
        full_cmd.add_simple_arg("--compare", compare)
        if ref is not None:
            full_cmd.append(ref)
        return stream_stdout_and_stderr(full_cmd, timeout=timeout)
