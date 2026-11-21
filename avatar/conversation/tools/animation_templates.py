"""Render a self-contained animation from model-selected scenes."""
from functools import lru_cache
from html import escape
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field

TEMPLATES = {
    '2d': {'timeline':'时间线','flow':'流程节点','wave':'叠加波形','orbits':'轨道周期','bars':'动态柱形',
           'packet':'网络传输','tree':'树遍历','spring':'弹簧振动','pendulum':'单摆','radar':'雷达扫描'},
    '3d': {'cube':'旋转立方体','layers':'分层爆炸图','helix':'双螺旋','surface':'波动曲面','solar':'行星系统',
           'lattice':'晶格网络','vortex':'粒子涡旋','torus':'环面网格','globe':'经纬球','arms':'机械臂关节'}
}


class Scene(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    label: str = Field(min_length=1,max_length=40)
    caption: str = Field(min_length=1,max_length=400)


@lru_cache(maxsize=1)
def player():
    return Path(__file__).with_name('animation_player.html').read_text(encoding='utf-8')


def render_animation(mode, template, title, scenes):
    if template not in TEMPLATES.get(mode,{}):
        raise ValueError('模板与动画维度不匹配')
    data = json.dumps({'template':template,'scenes':[s.model_dump() for s in scenes]},ensure_ascii=False)
    fields = {'TITLE':escape(title),'MODE':mode,'DATA':data.replace('<',r'\u003c'),
              'TRANSCRIPT':' '.join(escape(s.label+'：'+s.caption) for s in scenes)}
    return re.sub(r'__(TITLE|MODE|DATA|TRANSCRIPT)__',lambda m:fields[m[1]],player())
