"""Policy-aware conversion of malformed CLI JSON into `ParsingError`."""

from collections.abc import Callable
from dataclasses import dataclass

from pywhales.client_config import ParsingError

_SHAPE_ERRORS = (ValueError, LookupError, TypeError, AttributeError, RecursionError)


@dataclass(frozen=True, slots=True)
class JsonOutputParser:
    """Report parse failures of one CLI output surface by diagnostics policy.

    Build it where the diagnostics flag is read: at call time for one-shot
    parses, at launch for event streams. Only the callable handed to `parse()`
    is guarded, so launch, stream, and cleanup errors are never converted.

    Attributes:
        source: Fixed label for the surface; never derived from output or names.
        raw_diagnostics: Whether the original error is chained to `ParsingError`.
    """

    source: str
    raw_diagnostics: bool

    def parse[InputT, ResultT](
        self, parse: Callable[[InputT], ResultT], output: InputT
    ) -> ResultT:
        """Return `parse(output)`, or raise `ParsingError` for a bad payload.

        Args:
            parse: Decoder and validator for one payload.
            output: Captured CLI text or one stream record.

        Returns:
            The parsed value.

        Raises:
            ParsingError: If `parse` raises a decoding, validation, or shape
                error (`ValueError`, `LookupError`, `TypeError`,
                `AttributeError`), or exceeds the recursion limit (`RecursionError`).
                Safe mode withholds the payload and leaves neither a cause nor
                a context; raw mode chains the original.
        """
        try:
            return parse(output)
        except _SHAPE_ERRORS as err:
            if self.raw_diagnostics:
                raise ParsingError(f"Invalid {self.source} JSON response") from err
        raise ParsingError(f"Invalid {self.source} JSON response") from None
