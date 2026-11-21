"""密集/词法存储共用的中英文规范化词项。"""
import re
import unicodedata


def tokens(text):
    text=unicodedata.normalize('NFKC',text).casefold()
    words=re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+',text)
    return [token for word in words for token in ([word] if not re.fullmatch(r'[\u4e00-\u9fff]+',word) else list(word)+[word[i:i+2] for i in range(len(word)-1)])]
