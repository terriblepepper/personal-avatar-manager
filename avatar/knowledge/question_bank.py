"""平台仅保存清洗后的题目；抓取、翻译和清洗在 tools/question_bank 独立执行。"""
import hashlib
import json
import re
import secrets
from psycopg import IntegrityError
import unicodedata
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field,StrictBool
from typing import Literal
from avatar.accounts.auth import require_admin
from avatar.core.db import dump,event,now


class CategoryInput(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    id:str=Field(pattern=r'^[a-z][a-z0-9_-]{0,59}$')
    name:str=Field(min_length=1,max_length=60)
    active:StrictBool=False


class QuestionInput(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    id:str=Field(pattern=r'^[a-z][a-z0-9_-]{0,79}$')
    category:str=Field(pattern=r'^[a-z][a-z0-9_-]{0,59}$')
    prompt:str=Field(min_length=12,max_length=1200)
    source_url:str=Field(default='',max_length=500,pattern=r'^(?:https://[^\s]+)?$')
    source_id:str=Field(default='',max_length=80)
    original:str=Field(default='',max_length=500)
    license:str=Field(default='管理员原创',max_length=100)
    language:Literal['zh-CN']='zh-CN'
    active:StrictBool=True


class BankInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:Literal[1]=1
    name:str=Field(max_length=100)
    categories:list[CategoryInput]=Field(min_length=1,max_length=30)
    questions:list[QuestionInput]=Field(min_length=1,max_length=5000)


def fingerprint(text):
    return hashlib.sha256(re.sub(r'[^\w]','',unicodedata.normalize('NFKC',text).casefold()).encode()).hexdigest()


class QuestionBank:
    def __init__(self,database):self.database=database

    @staticmethod
    def categories(db):
        return [dict(r) for r in db.execute('''SELECT c.*,COUNT(q.id) AS total,
          COALESCE(SUM(q.active),0) AS available FROM soul_question_categories c
          LEFT JOIN soul_questions q ON q.category_id=c.id GROUP BY c.id ORDER BY c.name,c.id''')]

    @staticmethod
    def validate_active(db):
        for row in QuestionBank.categories(db):
            if row['active'] and row['available']<5:raise HTTPException(409,f'启用的类别“{row["name"]}”至少需要 5 道启用题目；可先停用类别再调整')

    def draw(self,db,count):
        categories=[row for row in self.categories(db) if row['active']]
        if not categories:raise HTTPException(409,'分类题库尚未准备好，请联系管理员导入题库')
        plan=[];rng=secrets.SystemRandom()
        for category in categories:
            rows=db.execute('SELECT * FROM soul_questions WHERE category_id=? AND active=1 ORDER BY id',(category['id'],)).fetchall()
            if len(rows)<count:raise HTTPException(409,f'类别“{category["name"]}”题量不足，请联系管理员')
            for index,row in enumerate(rng.sample(rows,count),1):
                plan.append({'id':row['id'],'category':category['id'],'category_name':category['name'],
                    'category_index':index,'category_total':count,'question':row['prompt'],'source':json.loads(row['source'])})
        return plan

    def page(self,user,category='',status='all',search='',offset=0):
        require_admin(user)
        clauses=[];args=[]
        if category:clauses.append('q.category_id=?');args.append(category)
        if status!='all':clauses.append('q.active=?');args.append(int(status=='active'))
        if search:clauses.append("strpos(lower(q.prompt||' '||q.id),lower(?))>0");args.append(search)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.database.connect() as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            total=db.execute('SELECT COUNT(*) FROM soul_questions q'+where,args).fetchone()[0]
            offset=min(offset,((max(total,1)-1)//10)*10)
            rows=db.execute('SELECT q.*,c.name AS category_name FROM soul_questions q JOIN soul_question_categories c ON c.id=q.category_id'+where+' ORDER BY q.created_at DESC,q.id LIMIT 10 OFFSET ?',(*args,offset)).fetchall()
            categories=self.categories(db)
        return {'categories':categories,'items':[{**dict(r),'source':json.loads(r['source'])} for r in rows],'total':total,'offset':offset}

    def import_package(self,user,package):
        require_admin(user)
        added=skipped=0
        with self.database.connect(write=True) as db:
            stamp=now()
            for item in package.categories:
                previous=db.execute('SELECT name FROM soul_question_categories WHERE id=?',(item.id,)).fetchone()
                if previous and previous[0]!=item.name:raise HTTPException(409,'类别标识已用于其他名称：'+item.id)
                if db.execute('SELECT 1 FROM soul_question_categories WHERE name=? AND id!=?',(item.name,item.id)).fetchone():raise HTTPException(409,'类别名称重复：'+item.name)
                db.execute('INSERT OR IGNORE INTO soul_question_categories VALUES(?,?,?,?,?)',(item.id,item.name,int(item.active),stamp,stamp))
            if db.execute('SELECT COUNT(*) FROM soul_question_categories').fetchone()[0]>30:raise HTTPException(422,'题库最多 30 个类别')
            for item in package.questions:
                if not db.execute('SELECT 1 FROM soul_question_categories WHERE id=?',(item.category,)).fetchone():raise HTTPException(422,'题目引用未知类别：'+item.category)
                marker=fingerprint(item.prompt)
                previous=db.execute('SELECT fingerprint FROM soul_questions WHERE id=?',(item.id,)).fetchone()
                if previous and previous[0]!=marker:raise HTTPException(409,'同一题目标识的内容不同，请使用编辑入口：'+item.id)
                if db.execute('SELECT 1 FROM soul_questions WHERE id=? OR fingerprint=?',(item.id,marker)).fetchone():skipped+=1;continue
                db.execute('INSERT INTO soul_questions VALUES(?,?,?,?,?,?,?,?)',(item.id,item.category,item.prompt,marker,dump(self.source(item)),int(item.active),stamp,stamp));added+=1
            self.validate_active(db)
            event(db,user['id'],user['username'],'导入 soul 分类题库',f'新增 {added}，跳过重复 {skipped}',actor_id=user['id'])
        return {'added':added,'skipped':skipped}

    @staticmethod
    def source(item):return {key:getattr(item,key) for key in ('source_url','source_id','original','license','language')}

    def save_category(self,user,item,create=False):
        require_admin(user)
        try:
            with self.database.connect(write=True) as db:
                if create:
                    if db.execute('SELECT COUNT(*) FROM soul_question_categories').fetchone()[0]>=30:raise HTTPException(422,'题库最多 30 个类别')
                    db.execute('INSERT INTO soul_question_categories VALUES(?,?,?,?,?)',(item.id,item.name,int(item.active),now(),now()))
                elif not db.execute('UPDATE soul_question_categories SET name=?,active=?,updated_at=? WHERE id=?',(item.name,int(item.active),now(),item.id)).rowcount:raise HTTPException(404,'类别不存在')
                self.validate_active(db)
                event(db,user['id'],user['username'],'维护 soul 题库类别',item.name,actor_id=user['id'])
        except IntegrityError:raise HTTPException(409,'类别名称或标识重复') from None
        return {'saved':True}

    def save_question(self,user,item,create=False):
        require_admin(user)
        try:
            with self.database.connect(write=True) as db:
                if not db.execute('SELECT 1 FROM soul_question_categories WHERE id=?',(item.category,)).fetchone():raise HTTPException(422,'类别不存在')
                values=(item.category,item.prompt,fingerprint(item.prompt),dump(self.source(item)),int(item.active),now())
                if create:db.execute('INSERT INTO soul_questions VALUES(?,?,?,?,?,?,?,?)',(item.id,*values[:-1],now(),now()))
                elif not db.execute('UPDATE soul_questions SET category_id=?,prompt=?,fingerprint=?,source=?,active=?,updated_at=? WHERE id=?',(*values,item.id)).rowcount:raise HTTPException(404,'题目不存在')
                self.validate_active(db)
                event(db,user['id'],user['username'],'维护 soul 题目',item.id,actor_id=user['id'])
        except IntegrityError:raise HTTPException(409,'题目标识或内容重复') from None
        return {'saved':True}
