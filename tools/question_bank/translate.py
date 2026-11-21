"""可选独立离线翻译，依赖 ctranslate2/sentencepiece，不调用平台或付费 API。"""
import argparse
import json
from pathlib import Path
import re
import time


def main():
 p=argparse.ArgumentParser();p.add_argument('candidates');p.add_argument('model');p.add_argument('output');p.add_argument('--limit',type=int,default=0);a=p.parse_args()
 import ctranslate2
 import sentencepiece
 root=Path(a.model);sp=sentencepiece.SentencePieceProcessor(model_file=str(root/'sentencepiece.model'))
 translator=ctranslate2.Translator(str(root/'model'),device='cpu',compute_type='int8',inter_threads=1,intra_threads=2)
 data=json.loads(Path(a.candidates).read_text())['items'];path=Path(a.output)
 cache=json.loads(path.read_text()) if path.exists() else {};pending=[x for x in data if x['source_id'] not in cache]
 if a.limit:pending=pending[:a.limit]
 started=time.monotonic()
 for offset in range(0,len(pending),32):
  batch=pending[offset:offset+32];inputs=[]
  for item in batch:
   text=item['original']
   if not re.match(r'^(I\b|My\b|It\b|There\b)',text):text='I '+text[0].lower()+text[1:]
   inputs.append(sp.encode(text,out_type=str))
  results=translator.translate_batch(inputs,beam_size=4,max_decoding_length=120)
  for item,result in zip(batch,results):cache[item['source_id']]=sp.decode(result.hypotheses[0])
  temp=path.with_suffix('.tmp');temp.write_text(json.dumps(cache,ensure_ascii=False,indent=2)+'\n');temp.replace(path)
  print(f'Translated {sum(x['source_id'] in cache for x in data)}/{len(data)} ({time.monotonic()-started:.1f}s)',flush=True)

if __name__=='__main__':main()
