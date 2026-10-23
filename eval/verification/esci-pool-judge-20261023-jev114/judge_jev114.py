"""jev-1.14 结构化判断：对 baseline 与查询理解版 top16 并集中的每个 (query, 商品) 单独判 E/S/C/I。
未标注对用于补判；已有人工 ESCI 标签的对同样盲判，用于计算与人工一致率（判官看不到人工标签）。
走本机 127.0.0.1:7890 代理调用 OpenRouter /api/alpha/decisions。"""
import json, os, sys, time, urllib.request, concurrent.futures as cf
V='/root/apps/globex-agent/eval/verification/'
BS=['product-recall-20261023-hybrid12-c64-k16','product-recall-20261023-query-understanding-c64-k16']
OUT=sys.argv[1]; RES=OUT+'/judgments.jsonl'
KEY=open('/root/.secrets/openrouter.key').read().strip()
URL='https://openrouter.ai/api/alpha/decisions'; MODEL='typesafe/jev-1.14'
WORKERS=int(os.environ.get('JUDGE_WORKERS','8')); LIMIT=int(os.environ.get('JUDGE_LIMIT','10**9'.replace('10**9','1000000000')))
opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:7890','http':'http://127.0.0.1:7890'}))
H=json.load(open('/tmp/bc/labels.json'))
pairs={}; queries={}
for b in BS:
    for s in ('hybrid_only','hybrid_rerank'):
        for l in open(V+b+f'/per_query_{s}.jsonl'):
            r=json.loads(l); queries[r['id']]=r['query']
            for p in r['retrieved'][:16]: pairs[(r['id'],p)]=None
need={p for _,p in pairs}; prod={}
with open('/root/datasets/esci-out/catalog_esci.jsonl') as f:
    for line in f:
        d=json.loads(line)
        if d['product_id'] in need: prod[d['product_id']]=d
def text(d):
    hl=d.get('highlights') or []
    if isinstance(hl,list): hl=' | '.join((h.get('detail') or json.dumps(h,ensure_ascii=False)) if isinstance(h,dict) else str(h) for h in hl[:3])
    return {'title':d['title'],'brand':d.get('brand') or '','highlights':str(hl)[:300],'description':str(d.get('description') or '')[:300]}
QUESTION={'esci':{'type':'choice',
  'instructions':'You are an Amazon search relevance annotator following the ESCI guidelines. Classify how the product relates to the shopping query. Judge only from the given product text. Respect every query specification, including type, brand, size, quantity, color and negations such as "without X".',
  'criteria':{'E':'Exact: the product is relevant and satisfies ALL query specifications.',
              'S':'Substitute: somewhat relevant; fails some specification but could serve as a functional substitute.',
              'C':'Complement: does not fulfil the query but could be used together with the queried item.',
              'I':'Irrelevant: irrelevant, or fails a central aspect of the query.'}}}
def call(q,d):
    body=json.dumps({'model':MODEL,'state':{'query':q,'product':text(d)},'questions':QUESTION}).encode()
    err=None
    for a in range(6):
        try:
            req=urllib.request.Request(URL,body,{'Authorization':'Bearer '+KEY,'Content-Type':'application/json'})
            r=json.load(opener.open(req,timeout=120)); ans=r['answers']['esci']
            return {'label':ans.get('choice'),'probabilities':ans.get('probabilities'),'confidence':ans.get('confidence'),
                    'model':r.get('model'),'provider':r.get('provider'),'cost':(r.get('usage') or {}).get('cost')}
        except Exception as e:
            err=type(e).__name__+':'+str(getattr(e,'code','')); time.sleep(min(60,3*2**a))
    raise RuntimeError(err)
done=set()
if os.path.exists(RES): done={(d['id'],d['product_id']) for d in map(json.loads,open(RES))}
todo=[k for k in pairs if k not in done and k[1] in prod][:LIMIT]
print('model',MODEL,'pairs total',len(pairs),'todo',len(todo),'missing text',sum(k[1] not in prod for k in pairs),flush=True)
def job(k):
    q,p=k; r=call(queries[q],prod[p])
    return {'id':q,'product_id':p,'human':H.get(q,{}).get(p),**r}
t0=time.time()
with open(RES,'a') as w, cf.ThreadPoolExecutor(WORKERS) as ex:
    futs={ex.submit(job,k):k for k in todo}
    for i,f in enumerate(cf.as_completed(futs),1):
        try: w.write(json.dumps(f.result(),ensure_ascii=False)+'\n'); w.flush()
        except Exception as e: print('FAIL',futs[f],str(e)[:80],flush=True)
        if i%200==0: print(i,'/',len(todo),f'{time.time()-t0:.0f}s',flush=True)
print('done',f'{time.time()-t0:.0f}s',flush=True)
