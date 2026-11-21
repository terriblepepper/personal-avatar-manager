# 模块说明：本地离线 OCR，仅用于文字兜底；不把图片上传第三方，也不执行文档内容。
import os
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path


# 职责：定位项目内 OCR 或系统 Tesseract，并限制线程数。
# 输入：系统环境及项目 .venv；不读取业务账号或网络配置。
# 结果与约束：优先使用本项目安装的中英文模型，缺失时返回不可用，不在请求中安装软件。
def runtime():
    local=Path(sys.prefix)/'share/avatar-ocr';binary=local/'usr/bin/tesseract'
    env={**os.environ,'OMP_THREAD_LIMIT':'1'}
    if binary.is_file():
        env['LD_LIBRARY_PATH']=os.pathsep.join(str(path) for path in (local/'usr/lib').glob('*-linux-gnu'))
        env['TESSDATA_PREFIX']=str(local/'usr/share/tesseract-ocr/5/tessdata')
        return str(binary),env
    return shutil.which('tesseract'),env


# 职责：识别图片中的中英文文字，保持有限内存和运行时长。
# 输入：已校验本地图片路径。
# 结果与约束：首帧、最多 1600 万像素、最长边 2400、每次识别八秒；返回空表示不能读出文字，不代表理解图片。
def image_text(path):
    binary,env=runtime()
    if not binary:return ''
    from PIL import Image,ImageOps
    with Image.open(path) as image:
        if image.width*image.height>16000000:raise ValueError('image pixels')
        image.seek(0);image=ImageOps.exif_transpose(image).convert('L');image.thumbnail((2400,2400));image=ImageOps.autocontrast(image)
        with tempfile.TemporaryDirectory(prefix='avatar-ocr-') as work:
            normalized=Path(work)/'input.png';image.save(normalized)
            result=subprocess.run([binary,str(normalized),'stdout','-l','chi_sim+eng','--psm','6'],env=env,capture_output=True,check=True,timeout=8)
    return result.stdout.decode('utf-8',errors='replace').strip()[:30000]


# 职责：按需把一个扫描 PDF 页面转为图片并识别。
# 输入：本地 PDF 和从零开始的页号，仅由有页数上限的提取器调用。
# 结果与约束：不联网，渲染最多五秒，图片最多 2000×2000；临时原图和结果自动清理。
def pdf_page_text(path,index):
    renderer=shutil.which('pdftoppm')
    if not renderer or not runtime()[0]:return ''
    with tempfile.TemporaryDirectory(prefix='avatar-pdf-ocr-') as work:
        prefix=Path(work)/'page'
        subprocess.run([renderer,'-f',str(index+1),'-l',str(index+1),'-singlefile','-scale-to','2000','-gray','-png',str(path),str(prefix)],capture_output=True,check=True,timeout=5)
        return image_text(prefix.with_suffix('.png'))
