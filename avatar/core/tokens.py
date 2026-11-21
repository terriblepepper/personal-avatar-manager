"""按 UTF-8 字节数保守估算上下文 Token；不作计费依据。"""
import math


def estimate(text):
    return math.ceil(len(text.encode('utf-8')) / 3)
