# 模块说明：将已授权的图片原件转为有限 JPEG 视觉输入，不执行 OCR、不保存额外副本。
import io
import warnings
import base64
from PIL import Image,ImageOps,UnidentifiedImageError
from fastapi import HTTPException


# 职责：统一聊天上传和个人资料图片的视觉编码，限制像素、尺寸和输出体积，剥离原 EXIF。
# 输入：source 为已完成归属校验的路径或二进制流；动图仅处理首帧。
# 结果与约束：返回最大 1 MiB JPEG 字节，不修改原件；损坏或过大图片明确返回 415。
def jpeg_bytes(source):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.width*image.height>16000000:raise ValueError()
                image.seek(0);image=ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((1600,1600))
                result=io.BytesIO();image.save(result,format='JPEG',quality=85)
                if result.tell()>1024*1024:
                    image.thumbnail((1024,1024));result=io.BytesIO();image.save(result,format='JPEG',quality=75)
                if result.tell()>1024*1024:raise ValueError()
                return result.getvalue()
    except (ValueError,OSError,UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise HTTPException(415,'图片损坏或超过视觉处理限制，请缩小后重新上传') from None


def image_input(path):
    return {'image':'data:image/jpeg;base64,'+base64.b64encode(jpeg_bytes(path)).decode()}
