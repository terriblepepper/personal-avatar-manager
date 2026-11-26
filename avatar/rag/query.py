"""受限自查询：模型只构造语义查询和元数据条件，权限范围始终由业务库决定。"""
from avatar.core.text_budget import AgentText, fits_text_budget, budget_error
from avatar.files.library.catalog import model_animation_metadata
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from fastapi import HTTPException


class FileQuery(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    query:AgentText=Field(min_length=1)
    alternatives:list[str]=Field(default_factory=list,max_length=2)
    file_ids:list[str]=Field(default_factory=list,max_length=5)
    filename:str=Field(default='',max_length=200)
    folder:str=Field(default='',max_length=1024,description='目录中的完整逻辑文件夹路径，限定该目录及全部子目录；留空表示不限定')
    file_type:Literal['','pdf','doc','docx','txt','md','csv','tsv','json','log','pptx','xlsx','image']=''
    section:str=Field(default='',max_length=200)
    page_start:int|None=Field(default=None,ge=1,le=100000)
    page_end:int|None=Field(default=None,ge=1,le=100000)
    source:Literal['','profile','chat']=''

    @model_validator(mode='after')
    def bounds(self):
        if self.folder:
            from avatar.files.library.folders import validate_path
            try:validate_path(self.folder)
            except HTTPException:raise ValueError('目录过滤路径无效') from None
        if self.page_start and self.page_end and self.page_end<self.page_start:raise ValueError('页码范围无效')
        if any(not isinstance(term,str) or not term.strip() for term in self.alternatives):raise ValueError('查询变体无效')
        if not fits_text_budget(self.query,*self.alternatives):raise ValueError(budget_error())
        if any(len(identifier)>80 for identifier in self.file_ids):raise ValueError('文件 ID 无效')
        return self

    def folder_matches(self,row):
        path=row.get('relative_path') or row.get('filename','')
        return not self.folder or path.startswith(self.folder+'/')

    def matches(self,metadata):
        if not self.folder_matches(metadata):return False
        if self.file_ids and metadata['file_id'] not in self.file_ids:return False
        if self.filename and self.filename.casefold() not in metadata['filename'].casefold():return False
        if self.section and self.section.casefold() not in metadata.get('section','').casefold():return False
        if self.source and self.source!=metadata['source']:return False
        extension=Path(metadata['filename']).suffix.lower().lstrip('.')
        if self.file_type and (extension not in ('png','jpg','jpeg','webp','gif','bmp') if self.file_type=='image' else extension!=self.file_type):return False
        page=metadata.get('page') or 0
        return not (self.page_start and page<self.page_start or self.page_end and (not page or page>self.page_end))



def file_catalog(documents,query=''):
    return [{'file_id':row['id'],'filename':row['filename'],'relative_path':row.get('relative_path') or row['filename'],
             'folder':(row.get('relative_path') or row['filename']).rpartition('/')[0],
             'source':row['source'],'description':row.get('caption',''),
             'content_type':row.get('content_type',''), **model_animation_metadata(row.get('animation_metadata'),query),
             'access':'conversation_reference' if row['source']=='chat' else 'library_search'} for row in documents]


def directory_catalog(documents):
    folders={}
    for row in documents:
        if row['source']!='profile':continue
        for folder in row.get('folder_descriptions',[]):
            folders[folder['path']]={'path':folder['path'],'parent':folder['path'].rpartition('/')[0],'description':folder['description']}
    return [folders[path] for path in sorted(folders)]
