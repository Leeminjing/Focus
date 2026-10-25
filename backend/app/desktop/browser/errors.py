"""Structured errors shared by the generic pipe codec and Focus adapter."""
class BrowserError(RuntimeError):
    def __init__(self, code, message, detail=None):
        self.code, self.detail = code, detail or {}
        super().__init__(f"{code}: {message}")

    def payload(self):
        return {"code": self.code, "message": str(self), "detail": self.detail}
