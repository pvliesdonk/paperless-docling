class ConfigurationError(Exception):
    """Raised when plugin configuration is invalid."""


class IncompatiblePaperlessError(Exception):
    """Raised when the running Paperless version is unsupported."""
