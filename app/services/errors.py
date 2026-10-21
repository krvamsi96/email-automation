"""Service-layer errors whose messages are safe to show to end users."""


class ServiceError(Exception):
    """A handled failure. ``message`` is user-facing; technical detail goes to logs only."""

    status = 400

    def __init__(self, message: str, code: str = "error", status: int = None):
        super().__init__(message)
        self.message = message
        self.code = code
        if status is not None:
            self.status = status


class NotFound(ServiceError):
    status = 404

    def __init__(self, message: str = "The requested item was not found.", code: str = "not_found"):
        super().__init__(message, code)


class Conflict(ServiceError):
    status = 409

    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(message, code)
