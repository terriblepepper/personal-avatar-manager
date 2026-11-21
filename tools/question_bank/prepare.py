"""独立公开题库清洗；不导入平台模块，不读取业务数据或用户模型配置。"""
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unicodedata

SOURCE='https://ipip.ori.org/AlphabeticalItemList.htm'
CATEGORIES=[
 ('expression','措辞与聊天表达',r'talk|word|speak|express|convers|joke|humor|laugh|story|stories|vocab|communicat|sarcas'),
 ('conflict','分歧与表达边界',r'argu|conflict|critic|insult|offend|forgiv|disagree|quarrel|revenge|angry|anger|annoy|temper|apolog|mistake|blame|confront|irritat'),
 ('empathy','共情与关怀',r'sympath|empath|comfort (?:others|people|someone)|comforting|kind\b|compassion|help|care for|care about|support|generous|charit|suffer|need.*others|others.*need|concern.*other|considerate|courteous|tender'),
 ('emotion','情绪与压力表达',r'emotion|mood|worr|anxious|anxiety|stress|calm|upset|nervous|relax|excit|fear|sad|happy|happin|cheer|optimis|pessimis|joy|disappoint|frustrat|panic'),
 ('planning','计划与做事习惯',r'work|task|plan(?:s|ned|ning)?\b|organiz|order|detail|schedule|finish|complet|persist|efficien|deadline|distract|concentrat|focus|tidy|neat|mess|prepared|prepare|goal|achiev|effort|accomplish|disciplin'),
 ('learning','学习与好奇心',r'learn|read|book|knowledg|intellect|understand|curious|curiosity|question|complex|theor|science|puzzl|think|thought|reason|logic|philosoph|analy|reflect|intellig|educat'),
 ('creativity','创意与审美偏好',r'\bart(?:s|istic)?\b|music|imagin|creativ|beaut|poe[mt]|paint|design|dream|fantas|aesthetic|invent|original|sing(?:ing)?\b|dance|danc|theater|theatre|novel'),
 ('decisions','决策与变化适应',r'decid|decis|risk|adventur|change|new|adapt|adjust|spontan|impuls|uncertain|unexpected|routine|consequenc|choos|choice|hesitat|opportun|experiment|explor'),
 ('values','原则与责任',r'honest|truth|fair(?:ness)?\b|justice|moral|conscien|princip|duty|duties|responsib|trust|loyal|promise|rule|respect|cheat|lie\b|lies\b|deceiv|reliab|dependab|integrity|value|for the sake'),
 ('social','社交与亲疏关系',r'friend|social|party|parties|crowd|group|people|others|other people|stranger|compan|team|lead|meeting|meet|shy|quiet|alone|popular|outgoing|assertiv|relationship|neighbor|neighbour|sociab|company|attention'),
 ('daily','日常偏好与精力',r'energy|energet|active|activit|busy|exercise|sport|walk|travel|outdoor|indoor|life|living|time|enjoy|pleasur|leisure|bored|bore|interest|hobb|play|rest|slow|fast|vigoro'),
 ('self','自我认知与反馈',r'confiden|feedback|self|proud|strength|weakness|skill|capab|aware|good at|identity|abilities|ability'),
]
EXCLUDE=re.compile(r'suicid|self.harm|kill|murder|die\b|death|dead|depress|hopeless|worthless|hallucin|psychot|schizo|paranoi|manic|panic attack|disorder|diagnos|mental.*ill|insan|crazy|voices|hearing things|evil|demon|devil|god\b|bible|church|religio|pray|spirit|faith|afterlife|reincarn|supernatural|destin|miracle|heaven|hell\b|politic|democra|republic|conservativ|liberal|vote|voting|government|nation|patriot|race\b|racial|ethnic|black|white|immigra|foreign|sex|romantic|marri|divorc|partner|spouse|husband|wife|women|woman|men\b|male|female|gender|pregnan|child|parent|mother|father|family|bodies|body|health|medical|disease|illness|symptom|pain|sick|sore|sleep|insomnia|\beat\b|food|weight|fat\b|thin|appetite|drink|alcohol|drunk|drug|tobacco|smok|gun|weapon|fight|violenc|violent|crim|steal|stole|theft|illegal|prison|law\b|laws\b|fraud|scam|money|wealth|rich|poor|income|bank|cash|financ|cheap|expens|possess|material things|gambl|bet\b|math|arithmet|calculat|number|counting|spell|chinese|french|latin|language|vocab.*tests|mental arithmetic|flashback|trauma|abuse|bully|hurting|tortur|cruel|disgust|hate|hatred|discriminat|worth.*others|superior|inferior|negative.*self|physique|deviant|salary|bless|thoughts.*audible|surveys|questionnaires|invisible|unreal|detached|out.of.body|stomach|heartbeat|breathe|breath|dizzy|faint|cramp|numb|skin\b|shak|trembl|sweat|persecut|nightmar|obsess|possess.*mind|mind.*read|read.*mind|special.*power|magical|magic|look.*mirror|ugly|pretty|attractiv|handsome|good.look|ugliness|beautiful person|swallow|rescuing|luxur|beliefs|social advancement|problems for myself',re.I)

class Table(HTMLParser):
 def __init__(self):super().__init__(convert_charrefs=True);self.rows=[];self.row=None;self.cell=None
 def handle_starttag(self,tag,attrs):
  if tag=='tr':self.row=[]
  elif tag=='td' and self.row is not None:self.cell=[]
 def handle_data(self,data):
  if self.cell is not None:self.cell.append(data)
 def handle_endtag(self,tag):
  if tag=='td' and self.cell is not None:
   self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
  elif tag=='tr' and self.row is not None:self.rows.append(self.row);self.row=None

def normal(text):return re.sub(r'[^\w]','',unicodedata.normalize('NFKC',text).casefold())
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def prepare(raw,output):
 parser=Table();parser.feed(Path(raw).read_text(errors='replace'));seen=set();items=[];rejected=[];tokens=[]
 for row in parser.rows:
  if len(row)<2:continue
  codes=re.findall(r'[A-Z]\d+',row[1])
  if not codes:continue
  text=unicodedata.normalize('NFKC',row[0]).replace('’',"'").replace('“','"').replace('”','"').strip();identifier=codes[0]
  reason=''
  if identifier.startswith('T'):reason='clinical_source_series'
  elif not 12<=len(text)<=180:reason='length'
  elif EXCLUDE.search(text):reason='outside_style_scope'
  elif normal(text) in seen:reason='exact_duplicate'
  else:
   words=set(re.findall(r'[a-z]+',text.casefold()))
   if any(len(words&other)/len(words|other)>=.92 for other in tokens):reason='near_duplicate'
  if reason:rejected.append({'source_id':identifier,'text':text,'reason':reason});continue
  category=next((key for key,label,pattern in CATEGORIES if re.search(r'\b(?:'+pattern+')',text,re.I)),None)
  if not category:
   rejected.append({'source_id':identifier,'text':text,'reason':'not_specific_to_style'});continue
  items.append({'source_id':identifier,'original':text,'category':category,'source_aliases':row[1]});seen.add(normal(text));tokens.append(words)
 if len(items)<1001:raise ValueError(f'Only {len(items)} candidates survive; inspect cleaning rules')
 write(output,{'source_url':SOURCE,'license':'Public Domain','raw_sha256':hashlib.sha256(Path(raw).read_bytes()).hexdigest(),'categories':[{'id':k,'name':v} for k,v,_ in CATEGORIES],'items':items,'rejected':rejected})
 print(json.dumps({'candidates':len(items),'rejected':len(rejected),'reasons':dict(Counter(x['reason'] for x in rejected)),'categories':dict(Counter(x['category'] for x in items))},ensure_ascii=False),flush=True)

def export(candidates,translations,output,report):
 data=json.loads(Path(candidates).read_text());translated=json.loads(Path(translations).read_text());items=[];seen=set();rejected=[]
 overrides_path=Path(candidates).parent/'review-overrides.json'
 overrides=json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
 for item in data['items']:
  text=unicodedata.normalize('NFKC',overrides.get(item['source_id'],translated.get(item['source_id'],''))).replace('▁',' ').strip().strip('。.!！ ')
  text=re.sub(r'(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])','',text).replace(',', '，')
  reason=''
  if not text or len(re.findall(r'[\u4e00-\u9fff]',text))<3:reason='missing_chinese'
  elif re.search(r'[a-zA-Z]{4,}|<[^>]*>|https?://',text):reason='translation_residue'
  elif re.search(r'自杀|自残|抑郁|幻觉|性欲|宗教|上帝|种族|薪水|薪资|工资|毒品|精神病',text):reason='translated_outside_scope'
  elif len(text)>110 or re.search(r'(.)\1{4,}',text):reason='translation_quality'
  elif normal(text) in seen:reason='translated_duplicate'
  if reason:rejected.append({**item,'translation':text,'reason':reason});continue
  seen.add(normal(text))
  prompt=f'关于“{text}”，这与你平时的习惯相符吗？请用平时聊天的语气说说原因，或举一个不涉及隐私的例子。'
  items.append({'id':'ipip-'+item['source_id'].lower(),'category':item['category'],'prompt':prompt,'source_url':SOURCE,'source_id':item['source_id'],'original':item['original'],'license':'Public Domain','language':'zh-CN'})
 counts=Counter(x['category'] for x in items)
 if len(items)<1001 or min(counts.values())<5:raise ValueError(f'Insufficient clean questions: {len(items)}, categories {counts}')
 package={'version':1,'name':'分身表达风格题库','categories':[{**x,'active':True} for x in data['categories']],'questions':items}
 write(output,package)
 write(report,{'source_url':SOURCE,'permission_url':'https://ipip.ori.org/newPermission.htm','license':'Public Domain','raw_sha256':data['raw_sha256'],'package_sha256':hashlib.sha256(Path(output).read_bytes()).hexdigest(),'created_at':datetime.now(timezone.utc).isoformat(),'original_count':len(data['items'])+len(data['rejected']),'candidate_count':len(data['items']),'accepted_count':len(items),'category_counts':dict(counts),'exclusion_counts':dict(Counter(x['reason'] for x in [*data['rejected'],*rejected])),'reviewed_corrections':len(overrides),'translation':'Argos en_zh 1.9 offline; adapted as open-ended style prompts, no psychometric validation','rejected':[*data['rejected'],*rejected]})
 print(json.dumps({'accepted':len(items),'categories':dict(counts)},ensure_ascii=False),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
 a=sub.add_parser('prepare');a.add_argument('raw');a.add_argument('output')
 a=sub.add_parser('export');a.add_argument('candidates');a.add_argument('translations');a.add_argument('output');a.add_argument('report')
 a=p.parse_args()
 if a.command=='prepare':prepare(a.raw,a.output)
 else:export(a.candidates,a.translations,a.output,a.report)
