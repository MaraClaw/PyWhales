"""Inspect and isolate-install both exact distribution files before sealing."""

import subprocess
import tempfile
import tomllib
from bisect import bisect_left
from email.parser import BytesParser
from pathlib import Path
from typing import Final

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from . import limits
from .api import Rejected, require
from .archives import tar_files, zip_files
from .budgets import local

SDIST_FILES: Final = frozenset(
    {
        "LICENSE",
        "MANIFEST.in",
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
        "setup.cfg",
        "pywhales.egg-info/PKG-INFO",
        "pywhales.egg-info/SOURCES.txt",
        "pywhales.egg-info/dependency_links.txt",
        "pywhales.egg-info/entry_points.txt",
        "pywhales.egg-info/requires.txt",
        "pywhales.egg-info/top_level.txt",
    }
)
RUNTIME_DEPENDENCIES: Final = frozenset({"orjson", "pydantic", "typing-extensions"})
ECR_MARKER: Final = Marker('extra == "ecr"')


def is_package_file(name: str) -> bool:
    """Allow only Python source and the package-level typing marker."""
    return name.startswith("pywhales/") and (
        name.endswith(".py") or name == "pywhales/py.typed"
    )


def directories_contain_files(directories: set[str], files: dict[str, bytes]) -> bool:
    """Allow explicit directory entries only as ancestors of accepted files."""
    paths = sorted(files)
    for directory in directories:
        prefix = f"{directory}/"
        index = bisect_left(paths, prefix)
        if index == len(paths) or not paths[index].startswith(prefix):
            return False
    return True


def inspect(files: dict[str, bytes], version: str) -> None:
    """Check actual archive contents and metadata without using the checkout."""
    wheel = f"pywhales-{version}-py3-none-any.whl"
    sdist = f"pywhales-{version}.tar.gz"
    require(set(files) == {wheel, sdist}, "distribution filenames")
    require(
        all(len(data) <= limits.DISTRIBUTION_BYTES for data in files.values()),
        "distribution input byte budget exceeded",
    )
    wheel_directories: set[str] = set()
    sdist_directories: set[str] = set()
    wheel_files = zip_files(
        files[wheel], limits.DISTRIBUTION, directories=wheel_directories
    )
    sdist_files = tar_files(
        files[sdist], limits.DISTRIBUTION, directories=sdist_directories
    )
    dist_info = f"pywhales-{version}.dist-info/"
    source_root = f"pywhales-{version}/"
    for contents, prefix, metadata in (
        (wheel_files, "", dist_info + "METADATA"),
        (sdist_files, source_root, source_root + "PKG-INFO"),
    ):
        require(prefix + "pywhales/py.typed" in contents, "missing py.typed")
        require(
            not any("python_on_whales" in name.split("/") for name in contents),
            "legacy package present",
        )
        require(metadata in contents, "missing distribution metadata")
        parsed = BytesParser().parsebytes(contents[metadata])
        require(
            parsed["Name"] == "pywhales" and parsed["Version"] == version,
            "distribution metadata mismatch",
        )
        for value in parsed.get_all("Requires-Dist", []):
            try:
                requirement = Requirement(str(value))
            except InvalidRequirement:
                raise Rejected("invalid distribution requirement") from None
            name = canonicalize_name(requirement.name)
            require(
                name in RUNTIME_DEPENDENCIES
                or (name == "botocore" and requirement.marker == ECR_MARKER),
                "unexpected distribution dependency",
            )
    require(
        all(is_package_file(name) or name.startswith(dist_info) for name in wheel_files)
        and directories_contain_files(wheel_directories, wheel_files),
        "unexpected wheel member",
    )
    for name in sdist_files:
        relative = name.removeprefix(source_root)
        require(
            relative != name and (relative in SDIST_FILES or is_package_file(relative)),
            "unexpected sdist member",
        )
    require(
        directories_contain_files(sdist_directories, sdist_files),
        "unexpected sdist member",
    )


def smoke(root: Path) -> None:
    """Install wheel and sdist separately outside the checkout with isolated Python."""
    root = root.resolve()
    version = tomllib.loads(
        local(root / "pyproject.toml", limits.PROJECT_BYTES).decode("utf-8")
    )["project"]["version"]
    # uv's output-directory marker is not part of the uploaded distributions.
    paths: dict[str, Path] = {}
    expected = {f"pywhales-{version}-py3-none-any.whl", f"pywhales-{version}.tar.gz"}
    for path in (root / "dist").iterdir():
        if path.name == ".gitignore":
            continue
        require(path.name in expected and path.is_file(), "distribution filenames")
        paths[path.name] = path
    require(set(paths) == expected, "distribution filenames")
    files = {
        name: local(path, limits.DISTRIBUTION_BYTES) for name, path in paths.items()
    }
    inspect(files, version)
    code = (
        "import importlib.metadata,importlib.util,json,pathlib,sys,pywhales;"
        "from importlib.resources import files;from pywhales import DockerClient;"
        "assert callable(DockerClient);"
        "distribution=importlib.metadata.distribution('pywhales');"
        "assert distribution.version==sys.argv[1];"
        "assert files('pywhales').joinpath('py.typed').is_file();"
        "assert importlib.util.find_spec('python_on_whales') is None;"
        "module=pathlib.Path(pywhales.__file__).resolve();"
        "assert distribution.files is not None;"
        "assert 'pywhales/__init__.py' in {str(file) for file in distribution.files};"
        "assert module==pathlib.Path(distribution.locate_file('pywhales/__init__.py')).resolve();"
        "assert json.loads(distribution.read_text('direct_url.json'))['url']==pathlib.Path(sys.argv[2]).as_uri();"
        "assert all(not module.is_relative_to(pathlib.Path(source).resolve()) for source in sys.argv[3:]);"
        "assert sys.flags.isolated"
    )
    with tempfile.TemporaryDirectory(prefix="delivery-smoke-") as temporary:
        for filename in sorted(files):
            subprocess.run(
                [
                    "uv",
                    "run",
                    "--isolated",
                    "--no-project",
                    "--no-config",
                    "--no-cache",
                    "--python",
                    "3.14.10",
                    "--with",
                    str(root / "dist" / filename),
                    "--",
                    "python",
                    "-I",
                    "-c",
                    code,
                    version,
                    str(root / "dist" / filename),
                    str(root),
                    str(Path(__file__).resolve().parents[2]),
                ],
                cwd=temporary,
                check=True,
                timeout=300,
            )
