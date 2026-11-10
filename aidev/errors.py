class PlatformError(Exception):
    def __init__(self, code, message, exit_code=2, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}


class OperationalError(PlatformError):
    def __init__(self, code, message, details=None):
        super().__init__(code, message, exit_code=3, details=details)


class AuthorizationError(PlatformError):
    """身份已认证但不被允许执行该操作（HTTP 403）。"""

    def __init__(self, code, message, details=None):
        super().__init__(code, message, exit_code=4, details=details)


class NotFoundError(PlatformError):
    """目标资源不存在（HTTP 404）。"""

    def __init__(self, code, message, details=None):
        super().__init__(code, message, exit_code=7, details=details)
