"""Sanitized errors permitted in the thin Longweis Relay client."""


class ProjectSError(Exception):
    """Base error shared by Relay client contracts."""


class ValidationError(ProjectSError):
    """Input did not satisfy a public contract."""


class IntegrityConflict(ProjectSError):
    """A persisted public object failed its integrity contract."""


class ProviderError(ProjectSError):
    """A configured model provider could not complete a request."""


class ProviderResponseError(ProviderError):
    """A provider returned a failed or malformed response."""

    def __init__(self, message: str, status_code=None) -> None:
        super().__init__(message)
        self.status_code = status_code
