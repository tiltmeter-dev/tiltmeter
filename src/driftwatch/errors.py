"""Exceptions raised by driftwatch."""


class DriftwatchError(Exception):
    """Base class for every error driftwatch raises on purpose."""


class ConfigError(DriftwatchError):
    """The configuration file is missing or invalid."""


class UnknownEndpointError(DriftwatchError):
    """An endpoint id that is not declared in the configuration."""

    def __init__(self, endpoint_id: str, known: list[str]) -> None:
        self.endpoint_id = endpoint_id
        self.known = known
        listed = ", ".join(known) if known else "none"
        super().__init__(f"unknown endpoint {endpoint_id!r} (configured endpoints: {listed})")


class MissingVariableError(DriftwatchError):
    """A ``${VAR}`` placeholder refers to an environment variable that is not set."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"environment variable {name} is not set")
