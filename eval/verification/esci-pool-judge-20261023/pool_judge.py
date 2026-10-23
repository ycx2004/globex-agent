"""池化补判：对两策略 top16 并集中无 ESCI 标签的商品用 LLM 判 E/S/C/I。
每个 query 一次调用，混入最多 8 个已有人工标签商品（盲测）用于计算与人工一致率。"""
import json, os, random, re, sys, time, urllib.request, concurrent.futures as cf
V='/root/apps/globex-agent/eval/verification/'
BS=os.environ.get('POOL_RUNS','product-recall-20261023-hybrid12-c64-k16').split(',')
PRIOR=os.environ.get('PRIOR_JUDGMENTS','')
OUT=sys.argv[1]; os.makedirs(OUT,exist_ok=True)
env={}
for l in open('/root/apps/globex-agent/.env'):
    if '=' in l and not l.startswith('#'): k,v=l.rstrip('\n').split('=',1); env[k]=v
URL=env['LLM_BASE_URL'].rstrip('/')+'/chat/completions'; KEY=env['LLM_API_KEY']; MODEL=env['LLM_MODEL']
labels=json.load(open('/tmp/bc/labels.json'))
runs={b+':'+s:{json.loads(l)['id']:json.loads(l) for l in open(V+b+f'/per_query_{s}.jsonl')} for b in BS for s in ('hybrid_only','hybrid_rerank')}
first=next(iter(runs.values()))
prior={}
if PRIOR:
    for l in open(PRIOR):
        d=json.loads(l); prior[d['id']]=set(d['judged'])
need=set()
for s in runs.values():
    for r in s.values(): need.update(r['retrieved'][:16])
for q in first: need.update(list(labels.get(q,{}))[:0])
prod={}
with open('/root/datasets/esci-out/catalog_esci.jsonl') as f:
    for line in f:
        d=json.loads(line)
        if d['product_id'] in need or True and False: pass
        prod[d['product_id']]=d if d['product_id'] in need else None
def text(pid):
    d=prod.get(pid)
    if not d:
        return None
    hl=d.get('highlights') or []
    hl=' | '.join((h.get('detail') or json.dumps(h,ensure_ascii=False)) if isinstance(h,dict) else str(h) for h in hl[:3]) if isinstance(hl,list) else str(hl)
    return f"Title: {d['title']}\nBrand: {d.get('brand','')}\nHighlights: {hl[:300]}\nDescription: {str(d.get('description',''))[:300]}"
SYS="""You are an Amazon search relevance annotator following the ESCI guidelines.
For a shopping query and each product, assign exactly one label:
E (Exact): the product is relevant and satisfies ALL query specifications (type, brand, size, quantity, color, negations such as "without X", etc.).
S (Substitute): somewhat relevant; fails some specification but could serve as a functional substitute.
C (Complement): does not fulfil the query but could be used together with the queried item.
I (Irrelevant): irrelevant, or fails a central aspect of the query.
Judge only from the given product text. Reply with JSON only: {"labels":{"<id>":"E|S|C|I",...}} covering every id."""
def call(msgs):
    body=json.dumps({'model':MODEL,'messages':msgs,'temperature':0,'response_format':{'type':'json_object'},'enable_thinking':False}).encode()
    for a in range(6):
        try:
            req=urllib.request.Request(URL,body,{'Authorization':'Bearer '+KEY,'Content-Type':'application/json'})
            r=json.load(urllib.request.urlopen(req,timeout=120))
            return r['choices'][0]['message']['content'], r.get('usage',{})
        except Exception as e:
            err=type(e).__name__+':'+str(getattr(e,'code',''))
            time.sleep(min(60,3*2**a))
    raise RuntimeError(err)
done=set()
res_path=OUT+'/judgments.jsonl'
if os.path.exists(res_path):
    done={json.loads(l)['id'] for l in open(res_path)}
def job(qid):
    q=first[qid]['query']; lab=labels.get(qid,{})
    pool=[]
    for s in runs.values():
        for p in s[qid]['retrieved'][:16]:
            if p not in pool: pool.append(p)
    unl=[p for p in pool if p not in lab and p not in prior.get(qid,set())]
    if not unl: return {'id':qid,'query':q,'judged':{},'calibration':{},'usage':{}}
    rng=random.Random(qid); known=[p for p in pool if p in lab]
    calib=rng.sample(known,min(8,len(known)))
    items=unl+calib; rng.shuffle(items)
    alias={f'p{i+1}':p for i,p in enumerate(items)}
    blocks='\n\n'.join(f"[{a}]\n{text(p)}" for a,p in alias.items() if text(p))
    content,usage=call([{'role':'system','content':SYS},{'role':'user','content':f'Query: {q}\n\nProducts:\n{blocks}'}])
    m=re.search(r'\{.*\}',content,re.S); got=json.loads(m.group(0)).get('labels',{})
    out={alias[a]:str(v).strip().upper()[:1] for a,v in got.items() if a in alias and str(v).strip().upper()[:1] in 'ESCI'}
    return {'id':qid,'query':q,'judged':{p:out.get(p) for p in unl},'calibration':{p:{'human':lab[p],'llm':out.get(p)} for p in calib},'usage':usage}
todo=[q for q in first if q not in done]
print('model',MODEL,'queries todo',len(todo),flush=True)
with open(res_path,'a') as w, cf.ThreadPoolExecutor(3) as ex:
    futs={ex.submit(job,q):q for q in todo}
    for i,f in enumerate(cf.as_completed(futs),1):
        try: w.write(json.dumps(f.result(),ensure_ascii=False)+'\n'); w.flush()
        except Exception as e: print('FAIL',futs[f],type(e).__name__,str(e)[:80],flush=True)
        if i%25==0: print(i,'/',len(todo),flush=True)
print('done',flush=True)
