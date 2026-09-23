class AgentError(Exception):
    """An actionable error safe to show to the operator."""


class ValidationError(AgentError):
    pass


class FieldValidationError(ValidationError):
    def __init__(self, field, expected):
        super().__init__(f"{field}：{expected}")
        self.field = field
        self.expected = expected


class CreativeRepairExhausted(ValidationError):
    def __init__(self, stage, detail):
        super().__init__("创作阶段格式修正次数已用完：" + detail)
        self.stage = stage


class ProviderError(AgentError):
    def __init__(self, message, *, retryable=False, uncertain=False):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain


class BusyError(AgentError):
    pass


class ModelOutputError(ProviderError):
    """A received, sanitized model response failed its output contract."""
    def __init__(self, message, *, kind, response, repairable=True):
        super().__init__(message)
        self.kind = kind
        self.response = response
        self.repairable = repairable
