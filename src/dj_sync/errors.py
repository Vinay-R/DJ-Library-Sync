class SyncError(Exception):
    """An actionable error whose message contains no raw remote data or credentials."""


class ApiError(SyncError):
    def __init__(self, service: str, status: int) -> None:
        self.status = status
        self.service = service
        hints = {
            401: "Token rejected; authenticate again.",
            403: "Access denied; check app entitlements, scopes, and playlist ownership.",
            404: "Resource missing or not accessible in this account/market.",
            429: "Rate limit reached; retry later.",
        }
        super().__init__(f"{service}: HTTP {status}. {hints.get(status, 'Request failed.')}")
