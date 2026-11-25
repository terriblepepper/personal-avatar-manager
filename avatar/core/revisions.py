# 模块说明：个人资料仅用实际修改日期标识内容，日期用于并发冲突检查和已审核记忆隔离，不生成数字版本号。
from datetime import datetime, timezone, timedelta
from typing import Annotated
from pydantic import AfterValidator, Field


# 职责：校验客户端传回的个人资料修改时间。
# 输入：value 为服务端此前返回的 ISO 8601 字符串。
# 结果与约束：必须含时区，保留原字符串供事务精确比较；不接受数字版本或无时区日期。
def validate_modified_at(value):
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('请传回个人资料的上次修改时间')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('个人资料修改时间必须包含时区')
    return value


ModifiedAt = Annotated[str, Field(min_length=20, max_length=64), AfterValidator(validate_modified_at)]


# 职责：生成严格晚于前次的个人资料修改时间。
# 输入：previous 为已有含时区修改日期，首次创建可为空。
# 结果与约束：正常使用当前 UTC 日期；同一微秒或时钟回拨时至少前进一微秒，防止不同编辑共用修改日期。
def next_modified_at(previous=''):
    current = datetime.now(timezone.utc)
    if previous:
        earlier = datetime.fromisoformat(previous)
        if current <= earlier:
            current = earlier + timedelta(microseconds=1)
    return current.isoformat(timespec='microseconds')
