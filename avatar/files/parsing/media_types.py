"""音视频格式与文件头校验；资料库与 Soul 共用，不调用解析模型。"""
from fastapi import HTTPException

MEDIA = {'.mp3':'audio/mpeg','.wav':'audio/wav','.m4a':'audio/mp4','.ogg':'audio/ogg',
         '.flac':'audio/flac','.webm':'video/webm','.mp4':'video/mp4','.mov':'video/quicktime'}


def validate_media(extension, data):
    valid = (data.startswith(b'RIFF') and data[8:12]==b'WAVE' if extension=='.wav' else
             data.startswith(b'fLaC') if extension=='.flac' else data.startswith(b'OggS') if extension=='.ogg' else
             data.startswith(b'\x1aE\xdf\xa3') if extension=='.webm' else
             data.startswith(b'ID3') or len(data)>1 and data[0]==255 and data[1]&224==224 if extension=='.mp3' else
             data[4:8] in (b'ftyp',b'moov',b'mdat',b'wide',b'free'))
    if not valid:raise HTTPException(415,'音视频文件内容与扩展名不匹配')
