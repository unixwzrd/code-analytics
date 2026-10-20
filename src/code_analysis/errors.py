from __future__ import annotations


class CodeAnalysisError(Exception):
    """Base class for expected, structured failures."""


class ConfigurationError(CodeAnalysisError):
    """Configuration or command input is invalid."""


class InvalidArgumentError(CodeAnalysisError):
    """A syntactically valid CLI argument has an invalid value."""


class ProviderExecutionError(CodeAnalysisError):
    """A requested provider could not produce a valid result."""


class ReportValidationError(CodeAnalysisError):
    """A persisted report does not satisfy the current schema."""
