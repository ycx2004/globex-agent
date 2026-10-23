"""用 人工 ESCI + jev-1.14 补判 重算 @8/@16；输出 jev 与人工、与 qwen 判官的一致率和配对 bootstrap。"""
import json, math, random
from collections import Counter
V='/root/apps/globex-agent/eval/verification/'; D=V+'esci-pool-judge-20261023-jev114/'
H=json.load(open('/tmp/bc/labels.json'))
R=[json.loads(l) for l in open(D+'judgments.jsonl')]
JJ={}; 
for r in R:
    if r['human'] is None and r['label']: JJ.setdefault(r['id'],{})[r['product_id']]=r['label']
JQ={}
for f in ('esci-pool-judge-20261023','esci-pool-judge-20261023-extra-qu'):
    for l in open(V+f+'/judgments.jsonl'):
        d=json.loads(l); JQ.setdefault(d['id'],{}).update({p:v for p,v in d['judged'].items() if v})
GAIN={'E':1,'S':0.1,'C':0.01,'I':0}
def per(r,k,J):
    q=r['id']; h=H.get(q,{}); j={p:v for p,v in (J or {}).get(q,{}).items() if p not in h}
    L=[h.get(p) or j.get(p) for p in r['retrieved'][:k]]; e=[x=='E' for x in L]
    nE=sum(v=='E' for v in h.values())+sum(v=='E' for v in j.values())
    dcg=sum(GAIN.get(x,0)/math.log2(i+2) for i,x in enumerate(L))
    ig=sorted([GAIN[v] for v in h.values()]+[GAIN[v] for v in j.values()],reverse=True)[:k]
    idcg=sum(g/math.log2(i+2) for i,g in enumerate(ig))
    return {'hit_E':any(e),'hit_ES':any(x in('E','S') for x in L),'precision_E':sum(e)/k,
            'recall_capped':sum(e)/min(nE,k) if nE else 0,'mrr':next((1/(i+1) for i,x in enumerate(e) if x),0),
            'ndcg_esci':dcg/idcg if idcg else 0,'unjudged':sum(x is None for x in L)/k}
def metrics(rows,k,J):
    a=Counter()
    for r in rows: a.update(per(r,k,J))
    return {m:round(v/len(rows),3) for m,v in a.items()}
RUNS=[('baseline@8','product-recall-20261023-hybrid12-c64-k8',8),('qu@8','product-recall-20261023-query-understanding-c64-k8',8),
      ('baseline@16','product-recall-20261023-hybrid12-c64-k16',16),('qu@16','product-recall-20261023-query-understanding-c64-k16',16)]
rows=lambda d,s:[json.loads(l) for l in open(V+d+f'/per_query_{s}.jsonl')]
out={'metrics':{}}
for name,d,k in RUNS:
    for s in ('hybrid_only','hybrid_rerank'):
        for jn,J in (('human',None),('qwen',JQ),('jev114',JJ)):
            key=f'{name} {s} {jn}'; out['metrics'][key]=metrics(rows(d,s),k,J); print(key.ljust(40),out['metrics'][key])
def cm(m,title):
    n=sum(m.values()); agree=sum(m[(x,x)] for x in 'ESCI')/n
    el=sum(m[(x,'E')] for x in 'ESCI'); eh=sum(m[('E',x)] for x in [*'ESCI',None])
    print(f'\n{title} (n={n}) agree={agree:.3f} E-precision={m[("E","E")]/el:.3f} humanE-recall={m[("E","E")]/eh:.3f}')
    print('row\\col    E     S     C     I  null')
    for h in 'ESCI': print(h.ljust(8),' '.join(str(m[(h,x)]).rjust(5) for x in [*'ESCI',None]))
    return {'n':n,'agree':round(agree,3),'matrix':{f'{a}->{b}':c for (a,b),c in m.items()}}
hm=Counter((r['human'],r['label']) for r in R if r['human'])
out['jev_vs_human']=cm(hm,'jev-1.14 vs human (human row)')
hi=Counter((r['human'],r['label']) for r in R if r['human'] and (r['confidence'] or 0)>=0.8)
out['jev_vs_human_conf80']=cm(hi,'jev-1.14 vs human, confidence>=0.8')
qj=Counter()
for q in JJ:
    for p,v in JJ[q].items():
        w=JQ.get(q,{}).get(p)
        if w: qj[(w,v)]+=1
out['qwen_vs_jev']=cm(qj,'qwen(row) vs jev-1.14(col) on unlabeled pairs')
print('\npaired bootstrap qu - baseline, hybrid_rerank, labels=human+jev114')
out['bootstrap']={}
for k,b,c in [(8,RUNS[0][1],RUNS[1][1]),(16,RUNS[2][1],RUNS[3][1])]:
    A={r['id']:r for r in rows(b,'hybrid_rerank')}; B={r['id']:r for r in rows(c,'hybrid_rerank')}
    for met in ('recall_capped','ndcg_esci','mrr','hit_E','precision_E'):
        d=[per(B[i],k,JJ)[met]-per(A[i],k,JJ)[met] for i in sorted(A)]
        g=random.Random(0); bs=sorted(sum(g.choice(d) for _ in d)/len(d) for _ in range(2000))
        res={'delta':round(sum(d)/len(d),4),'ci95':[round(bs[50],4),round(bs[1949],4)],'better':sum(x>0 for x in d),'worse':sum(x<0 for x in d)}
        out['bootstrap'][f'@{k} {met}']=res; print(f'@{k} {met}',res)
out['cost_usd']=round(sum(r['cost'] or 0 for r in R),4); out['models']=dict(Counter(r['model'] for r in R)); out['pairs']=len(R)
print('\ncost',out['cost_usd'],'models',out['models'],'pairs',len(R))
json.dump(out,open(D+'rescore.json','w'),indent=1,ensure_ascii=False)
