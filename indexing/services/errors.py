"""共享业务错误；入口负责 HTTP、MCP 或终端呈现。"""


class BusinessError(ValueError):
    def __init__(self, code: str, message: str, *, data=None):
        super().__init__(message)
        self.code = code
        self.data = data
