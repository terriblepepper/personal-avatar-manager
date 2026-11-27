"""Stable error types shared by identity validation and registration adapters."""


# 职责：表示本体待审、证书暂不可用等可以恢复的注册等待状态。
# 输入：message 为应用固定提示，phase 是有限注册阶段。
# 结果与约束：Worker 延迟重试同一身份目录；不将等待当成功，也不删除远端注册状态。
class RegistrationPending(RuntimeError):
    # 职责：保存安全提示及下一次尝试的等待时间。
    # 输入：message 不得含远端正文或凭据，delay 以秒计。
    # 结果与约束：最少等待一秒，避免队列忙循环。
    def __init__(self, message, delay=30, *, phase="waiting"):
        self.phase = phase
        self.delay = max(1, delay)
        super().__init__(message)


# 职责：表示需要运维修复或对账后才能人工重试的注册失败。
# 输入：message 为固定中文提示，不能包含证书私钥或远端响应。
# 结果与约束：沿用原状态和 AIC，禁止以删除目录方式自动重新申请。
class RegistrationFailure(RuntimeError):
    def __init__(self,message,*,phase="failed"):
        self.phase=phase
        super().__init__(message)


# 职责：用稳定错误码表达注册前置条件，不把密码或远端响应写进异常。
# 输入：code 为应用定义的配置/协议错误标识。
# 结果与约束：供后台显示固定中文说明，不能把异常正文作为用户提示或自动重新注册依据。
class RegistrationConfigurationError(ValueError):
    # 职责：保存可公开给运维的稳定错误码。
    # 输入：code 为不含凭据的短字符串。
    # 结果与约束：异常消息仅含错误码；不携带原始 ACS、HTTP 正文或 Token。
    def __init__(self, code):
        self.code = code
        super().__init__(code)
