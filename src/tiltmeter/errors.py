"""Exceptions raised by Tiltmeter."""


class TiltmeterError(Exception):
    """Base class for every error Tiltmeter raises on purpose."""


class ConfigError(TiltmeterError):
    """The configuration file is missing or invalid."""


class UnknownEndpointError(TiltmeterError):
    """An endpoint id that is not declared in the configuration."""

    def __init__(self, endpoint_id: str, known: list[str]) -> None:
        self.endpoint_id = endpoint_id
        self.known = known
        listed = ", ".join(known) if known else "none"
        super().__init__(f"unknown endpoint {endpoint_id!r} (configured endpoints: {listed})")


class MissingVariableError(TiltmeterError):
    """A ``${VAR}`` placeholder refers to an environment variable that is not set."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"environment variable {name} is not set")


class FixError(TiltmeterError):
    """``tiltmeter fix`` cannot go on: no repository, no credentials, a git failure."""


class HarError(TiltmeterError):
    """A file that ``tiltmeter init --from-har`` cannot read as a HAR export."""


class NoticeError(TiltmeterError):
    """A change notice, feed or signing key that cannot be read, trusted or written."""
