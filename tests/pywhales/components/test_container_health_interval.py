import json
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales.utils import ProcessStream


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("mode", ["create", "captured", "detached", "streamed"])
@pytest.mark.parametrize("interval", [None, 3, timedelta(seconds=3)])
def test_health_interval_reaches_cli_in_each_mode(
    tmp_path: Path, alias: bool, mode: str, interval: int | timedelta | None
) -> None:
    driver = tmp_path / "driver.py"
    record = tmp_path / "argv.jsonl"
    driver.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[2:]\n"
        "with Path(sys.argv[1]).open('a') as output:\n"
        "    output.write(json.dumps(args) + '\\n')\n"
        "if args[:2] == ['container', 'inspect']:\n"
        '    print(\'[ {"Id":"created-id"} ]\')\n'
        "else:\n"
        "    print('created-id')\n"
    )
    client = DockerClient(client_call=[sys.executable, str(driver), str(record)])
    if mode == "create":
        create = client.create if alias else client.container.create
        result = create("app", pull="never", health_start_interval=interval)
    else:
        run = client.run if alias else client.container.run
        result = run(
            "app",
            pull="never",
            health_start_interval=interval,
            detach=mode == "detached",
            stream=mode == "streamed",
        )
    if isinstance(result, ProcessStream):
        with result:
            list(result)
    launches = [json.loads(line) for line in record.read_text().splitlines()]
    argv = launches[0]
    assert "--health-cmd" not in argv
    assert "--health-start-period" not in argv
    if interval is None:
        assert "--health-start-interval" not in argv
    else:
        assert argv[argv.index("--health-start-interval") + 1] == "3s"


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("mode", ["create", "captured", "detached", "streamed"])
@pytest.mark.parametrize(
    "interval",
    [
        True,
        False,
        0,
        -1,
        timedelta(),
        timedelta(seconds=-1),
        timedelta(microseconds=1),
        timedelta(seconds=1, microseconds=1),
    ],
)
def test_invalid_health_interval_precedes_preflight(
    alias: bool, mode: str, interval: int | timedelta
) -> None:
    client = DockerClient()
    with (
        patch("shutil.which") as lookup,
        patch("pywhales.components.image.cli_wrapper.ImageCLI") as image,
        pytest.raises(ValueError),
    ):
        if mode == "create":
            create = client.create if alias else client.container.create
            create("app", pull="always", health_start_interval=interval)
        else:
            run = client.run if alias else client.container.run
            run(
                "app",
                pull="always",
                detach=mode == "detached",
                stream=mode == "streamed",
                health_start_interval=interval,
            )
    lookup.assert_not_called()
    image.assert_not_called()
