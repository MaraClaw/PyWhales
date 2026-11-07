"""Exact CHANGELOG section extraction and release-body comparison.

Headings are matched by string equality, never by pattern, because versions
such as ``0.7.0rc1`` contain regular-expression metacharacters. Only top-level
CommonMark fences (0-3 spaces of indent) hide headings; containers are not parsed.
"""

from . import limits
from .api import require

HEADING = "## "


def unix_lines(text: str) -> list[str]:
    """Split on LF after folding CRLF and CR, never on other Unicode separators."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def opening(line: str) -> str | None:
    """Return the fence run opening a top-level fenced code block, if any."""
    body = line.lstrip(" ")
    char = body[:1]
    if len(line) - len(body) > 3 or char not in {"`", "~"}:
        return None
    run = len(body) - len(body.lstrip(char))
    if run < 3 or (char == "`" and "`" in body[run:]):
        return None
    return body[:run]


def closes(line: str, opener: str) -> bool:
    """Whether the line closes the fence: same character, no shorter, then blanks."""
    body = line.lstrip(" ")
    if len(line) - len(body) > 3:
        return False
    body = body.rstrip(" \t")
    return len(body) >= len(opener) and body == opener[0] * len(body)


def fenced(lines: list[str]) -> list[bool]:
    """Mark every line of each top-level fence, delimiters included.

    The scan starts at the first line of the file, so a fence opened before the
    version heading is honored. An unclosed fence anywhere is rejected.
    """
    marks: list[bool] = []
    opener: str | None = None
    for line in lines:
        if opener is None:
            opener = opening(line)
            marks.append(opener is not None)
        else:
            marks.append(True)
            if closes(line, opener):
                opener = None
    require(opener is None, "release notes fence unclosed")
    return marks


def section(text: str, version: str) -> str:
    """Return the body under the one level-2 heading that is exactly the version.

    The body runs to the next level-2 heading or the end of the file, keeps its
    deeper subheadings, and excludes the heading and surrounding blank lines.
    Lines inside top-level fenced code blocks are never headings.
    """
    lines = unix_lines(text)
    marks = fenced(lines)
    heading = f"{HEADING}{version}"
    starts = [
        index
        for index, line in enumerate(lines)
        if line == heading and not marks[index]
    ]
    require(bool(starts), "release notes section missing")
    require(len(starts) == 1, "release notes section duplicated")
    body = lines[starts[0] + 1 :]
    end = next(
        (
            index
            for index, (line, mark) in enumerate(
                zip(body, marks[starts[0] + 1 :], strict=True)
            )
            if line.startswith(HEADING) and not mark
        ),
        len(body),
    )
    filled = [index for index, line in enumerate(body[:end]) if line.strip()]
    require(bool(filled), "release notes section empty")
    notes = "\n".join(body[filled[0] : filled[-1] + 1])
    require(len(notes.encode()) <= limits.NOTES_BYTES, "release notes size budget")
    return notes


def normalized(text: str | None) -> str:
    """Fold line endings and drop trailing whitespace per line and at the end."""
    return "\n".join(line.rstrip() for line in unix_lines(text or "")).rstrip()


def matches(published: str | None, sealed: str) -> bool:
    """Compare a release body with the sealed notes, ignoring only that drift."""
    return normalized(published) == normalized(sealed)
