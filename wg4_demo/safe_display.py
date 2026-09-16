"""Safe user-facing error text; raw exceptions and secrets stay server-side."""

from __future__ import annotations

from wg4_demo.errors import AppError

GENERIC_ERROR = "処理を完了できませんでした。操作IDとエラーコードを運営者へ伝えてください。"


def safe_error(error: BaseException) -> tuple[str, str]:
    if isinstance(error, AppError):
        return error.code, error.user_message
    return "internal_error", GENERIC_ERROR
