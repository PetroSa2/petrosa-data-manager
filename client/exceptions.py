"""
Custom exceptions for the Data Manager Client.
"""


class DataManagerError(Exception):
    """Base exception for all Data Manager client errors."""

    pass


class ConnectionError(DataManagerError):
    """Raised when connection to Data Manager fails."""

    pass


class TimeoutError(DataManagerError):
    """Raised when a request times out."""

    pass


class ValidationError(DataManagerError):
    """Raised when data validation fails."""

    pass


class APIError(DataManagerError):
    """Raised when API returns an error response."""

    def __init__(self, message: str, status_code: int = None, response: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response
