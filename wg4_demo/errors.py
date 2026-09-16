"""Safe, typed failures used at service and job boundaries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AppError(Exception):
    code: str
    user_message: str
    stage: str
    indeterminate: bool = False

    def __str__(self) -> str:
        return self.code


class ConfigurationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("configuration_error", message, "configuration")


class AuthenticationError(AppError):
    def __init__(
        self, code: str = "authentication_failed", message: str = "認証できませんでした。"
    ) -> None:
        super().__init__(code, message, "authentication")


class AuthorizationError(AppError):
    def __init__(self, message: str = "この操作を実行する権限がありません。") -> None:
        super().__init__("authorization_failed", message, "authorization")


class ValidationFailure(AppError):
    def __init__(self, message: str = "結果を検証できなかったため、保存・表示しません。") -> None:
        super().__init__("validation_failed", message, "validation")


class SearchFailure(AppError):
    def __init__(
        self, message: str = "検索処理に失敗したため、根拠の有無を判定できません。"
    ) -> None:
        super().__init__("search_failed", message, "search")


class StaleContextError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "stale_context",
            "待機中に知識版または会話が変わったため、この操作を終了しました。",
            "context_check",
        )


class IndeterminateError(AppError):
    def __init__(self, code: str, message: str, stage: str) -> None:
        super().__init__(code, message, stage, indeterminate=True)
