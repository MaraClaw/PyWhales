"""Recording fake Docker CLI for exact-argv tests; never launches Docker."""

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pywhales import DockerClient

_SCRIPT = """\
import json, os, sys, time
log, rules_path = sys.argv[1], sys.argv[2]
argv = sys.argv[3:]
record = {"argv": argv}
with open(rules_path) as source:
    rules = json.load(source)
for rule in rules:
    if all(token in argv for token in rule["match"]):
        if rule["read_stdin"]:
            record["stdin_bytes"] = len(sys.stdin.buffer.read())
        if rule["env"]:
            record["env"] = {name: os.environ.get(name) for name in rule["env"]}
        with open(log, "a") as output:
            output.write(json.dumps(record) + "\\n")
        sys.stdout.write(rule["stdout"])
        sys.stdout.flush()
        sys.stderr.write(rule["stderr"])
        sys.stderr.flush()
        if rule["sleep"]:
            time.sleep(rule["sleep"])
        sys.exit(rule["exit_code"])
with open(log, "a") as output:
    output.write(json.dumps(record) + "\\n")
"""


@dataclass(frozen=True)
class Rule:
    """Response for the first invocation whose argv contains every match token.

    Attributes:
        match: Tokens that must all appear in argv, in any order.
        stdout: Text written to stdout.
        stderr: Text written to stderr.
        exit_code: Process exit status.
        sleep: Seconds to sleep after writing output, for deadline tests.
        read_stdin: Record the number of stdin bytes read before responding.
        env: Environment names whose values are recorded.
    """

    match: Sequence[str] = ()
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    sleep: float = 0
    read_stdin: bool = False
    env: Sequence[str] = ()


def fake_cli_call(tmp_path: Path, rules: Sequence[Rule] = ()) -> tuple[list[str], Path]:
    """Write a recording Python CLI and return its client_call prefix.

    Use this to build a DockerClient with extra settings.

    Args:
        tmp_path: Directory receiving the script, rules, and call log.
        rules: Ordered responses; unmatched calls exit zero with no output.

    Returns:
        The client_call prefix and the JSON-lines call log path.
    """
    script = tmp_path / "fake_docker_cli.py"
    rules_path = tmp_path / "fake_docker_rules.json"
    log = tmp_path / "fake_docker_calls.jsonl"
    script.write_text(_SCRIPT)
    rules_path.write_text(
        json.dumps(
            [
                {
                    "match": list(rule.match),
                    "stdout": rule.stdout,
                    "stderr": rule.stderr,
                    "exit_code": rule.exit_code,
                    "sleep": rule.sleep,
                    "read_stdin": rule.read_stdin,
                    "env": list(rule.env),
                }
                for rule in rules
            ]
        )
    )
    return [sys.executable, str(script), str(log), str(rules_path)], log


def make_fake_cli(
    tmp_path: Path, rules: Sequence[Rule] = ()
) -> tuple[DockerClient, Path]:
    """Return a default client whose executable is a recording Python script.

    Args:
        tmp_path: Directory receiving the script, rules, and call log.
        rules: Ordered responses; unmatched calls exit zero with no output.

    Returns:
        The client and the JSON-lines call log path.
    """
    client_call, log = fake_cli_call(tmp_path, rules)
    return DockerClient(client_call=client_call), log


def records(log: Path) -> list[dict[str, object]]:
    """Return every recorded invocation, oldest first."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def calls(log: Path) -> list[list[str]]:
    """Return the argv after the executable prefix for every invocation."""
    result: list[list[str]] = []
    for line in log.read_text().splitlines() if log.exists() else ():
        argv = json.loads(line)["argv"]
        result.append([str(token) for token in argv])
    return result
