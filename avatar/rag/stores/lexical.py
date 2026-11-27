"""Persistent word statistics; server-owned active manifests gate every FTS hit."""
import json
from avatar.rag.stores.tokenization import tokens


# 职责：把规范化词项拼成 OR 语义的 PostgreSQL tsquery。
# 输入：terms 来自 tokens()，只含小写字母、数字、下划线和汉字。
# 结果与约束：返回 to_tsquery 可解析的字符串；调用方保证 terms 非空。
def _tsquery(terms):
    return ' | '.join(term for term in terms if term)


class LexicalStore:
    def __init__(self,database):
        self.database=database

    def stage(self,user_id,collection,records):
        with self.database.connect(write=True, account_scope=user_id) as db:
            self.stage_in(db,user_id,collection,records)

    @staticmethod
    def stage_in(db,user_id,collection,records):
        ids,metadata,texts=records
        for identifier,meta,text in zip(ids,metadata,texts,strict=True):
            terms=' '.join(tokens(meta.get('filename','')+' '+meta.get('section','')+' '+text))
            db.execute("""INSERT INTO rag_lexical_records(user_id,collection,record_id,file_id,source,text,metadata,tokens)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,collection,record_id) DO UPDATE SET
                file_id=excluded.file_id,source=excluded.source,text=excluded.text,metadata=excluded.metadata,tokens=excluded.tokens""",
                (user_id,collection,identifier,meta['file_id'],meta['source'],text,json.dumps(meta,ensure_ascii=False),terms))

    def contains(self,user_id,collection,identifiers):
        if not identifiers:return False
        with self.database.connect() as db:
            count=db.execute("""SELECT count(*) FROM rag_lexical_records
                WHERE user_id=? AND collection=? AND record_id=ANY(?::text[])
                  AND (tokens='' OR to_tsvector('simple',tokens)
                       @@ to_tsquery('simple',split_part(tokens,' ',1)))""",
                (user_id,collection,list(identifiers))).fetchone()[0]
        return count==len(set(identifiers))

    def search(self,user_id,collection,allowed_ids,queries,limit=36):
        if not allowed_ids:return []
        found={};allowed=list(allowed_ids)
        with self.database.connect() as db:
            for query in queries:
                terms=list(dict.fromkeys(tokens(query)))[:64]
                if not terms:continue
                rows=db.execute("""SELECT r.record_id,r.text,r.metadata,
                    -ts_rank_cd(to_tsvector('simple',r.tokens),to_tsquery('simple',?)) AS rank
                    FROM rag_lexical_records r
                    JOIN file_rag_nodes n ON n.user_id=r.user_id AND n.collection=r.collection AND n.record_id=r.record_id
                    JOIN file_rag_documents d ON d.source=n.source AND d.file_id=n.file_id
                    JOIN rag_publications p ON p.id=d.active_publication AND p.lexical_ready=1
                    WHERE to_tsvector('simple',r.tokens) @@ to_tsquery('simple',?)
                      AND r.user_id=? AND r.collection=?
                      AND r.record_id=ANY(?::text[])
                    ORDER BY rank,r.record_id LIMIT ?""",
                    (_tsquery(terms),_tsquery(terms),user_id,collection,allowed,max(1,min(limit,128)))).fetchall()
                for row in rows:
                    if row['record_id'] not in found:found[row['record_id']]={'id':row['record_id'],'text':row['text'],'metadata':json.loads(row['metadata']),'lexical_score':row['rank']}
        return sorted(found.values(),key=lambda row:(row['lexical_score'],row['id']))[:limit]

    def delete_in(self,db,user_id,collection,identifiers):
        db.execute('DELETE FROM rag_lexical_records WHERE user_id=? AND collection=? AND record_id=ANY(?::text[])',(user_id,collection,list(identifiers)))
