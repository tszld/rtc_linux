class ServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class NotFoundError(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 404)


class ForbiddenError(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 403)


class ConflictError(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 409)


class UnavailableError(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 503)
