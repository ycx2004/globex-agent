"""用人工 ESCI + jev-router 补判重算指标，并与 qwen 补判对照。"""
import json, math, random
from collections import Counter
V='/root/apps/globex-agent/eval/verification/'
H=json.load(open('/tmp/bc/labels.json'))
def load(fs):
    J={};C=[]
    for f in fs:
        for l in open(V+f+'/judgments.jsonl'):
            d=json.loads(l); J.setdefault(d['id'],{}).update({p:v for p,v in d['judged'].items() if v}); C+=d['calibration'].values()
    return J,C
JQ,CQ=load(['esci-pool-judge-20261023','esci-pool-judge-20261023-extra-qu'])
JJ,CJ=load(['esci-pool-judge-20261023-jev'])
GAIN={'E':1,'S':0.1,'C':0.01,'I':0}
def metrics(rows,k,J):
    agg=Counter()
    for r in rows:
        q=r['id']; h=H.get(q,{}); j={p:v for p,v in J.get(q,{}).items() if p not in h} if J is not None else {}
        L=[h.get(p) or j.get(p) for p in r['retrieved'][:k]]; e=[x=='E' for x in L]
        nE=sum(v=='E' for v in h.values())+sum(v=='E' for v in j.values())
        dcg=sum(GAIN.get(x,0)/math.log2(i+2) for i,x in enumerate(L))
        ig=sorted([GAIN[v] for v in h.values()]+[GAIN[v] for v in j.values()],reverse=True)[:k]
        idcg=sum(g/math.log2(i+2) for i,g in enumerate(ig))
        m={'hit_E':any(e),'hit_ES':any(x in('E','S') for x in L),'recall_capped':sum(e)/min(nE,k) if nE else 0,
           'mrr':next((1/(i+1) for i,x in enumerate(e) if x),0),'ndcg_esci':dcg/idcg if idcg else 0,'unjudged':sum(x is None for x in L)/k}
        agg.update(m)
    return {a:round(b/len(rows),3) for a,b in agg.items()}
RUNS=[('baseline@8','product-recall-20261023-hybrid12-c64-k8',8),('qu@8','product-recall-20261023-query-understanding-c64-k8',8),
      ('baseline@16','product-recall-20261023-hybrid12-c64-k16',16),('qu@16','product-recall-20261023-query-understanding-c64-k16',16)]
rows=lambda d,s:[json.loads(l) for l in open(V+d+f'/per_query_{s}.jsonl')]
out={}
for name,d,k in RUNS:
    for s in ('hybrid_only','hybrid_rerank'):
        for jn,J in (('human',None),('qwen',JQ),('jev',JJ)):
            key=f'{name} {s} {jn}'; out[key]=metrics(rows(d,s),k,J); print(key.ljust(36),out[key])
def cm(C,name):
    m=Counter((c['human'],c['llm']) for c in C); n=sum(m.values())
    print(f'\n{name} vs human (n={n}) agree={sum(m[(x,x)] for x in "ESCI")/n:.3f}')
    print('human\\llm  E    S    C    I   null')
    for h in 'ESCI': print(h.ljust(9),' '.join(str(m[(h,x)]).rjust(4) for x in [*'ESCI',None]))
    el=sum(m[(x,'E')] for x in 'ESCI'); eh=sum(m[('E',x)] for x in [*'ESCI',None])
    print(f'E precision {m[("E","E")]/el:.3f}  human-E recall {m[("E","E")]/eh:.3f}')
cm(CQ,'qwen3.8-flash'); cm(CJ,'jev-router')
# 两个判官在同一未标注对上的一致率
both=Counter()
for q in JJ:
    for p,v in JJ[q].items():
        w=JQ.get(q,{}).get(p)
        if w: both[(w,v)]+=1
n=sum(both.values()); print(f'\nqwen vs jev on {n} unlabeled pairs agree={sum(both[(x,x)] for x in "ESCI")/n:.3f}; E-binary agree={sum(c for (a,b),c in both.items() if (a=="E")==(b=="E"))/n:.3f}')
print('qwen\\jev  ',' '.join(x.rjust(4) for x in 'ESCI'))
for a in 'ESCI': print(a.ljust(9),' '.join(str(both[(a,x)]).rjust(4) for x in 'ESCI'))
print('\npaired bootstrap qu - baseline (hybrid_rerank, jev labels)')
for k,b,c in [(8,RUNS[0][1],RUNS[1][1]),(16,RUNS[2][1],RUNS[3][1])]:
    A={r['id']:r for r in rows(b,'hybrid_rerank')}; B={r['id']:r for r in rows(c,'hybrid_rerank')}
    for met in ('recall_capped','ndcg_esci','mrr'):
        d=[metrics([B[i]],k,JJ)[met]-metrics([A[i]],k,JJ)[met] for i in sorted(A)]
        r=random.Random(0); bs=sorted(sum(r.choice(d) for _ in d)/len(d) for _ in range(2000))
        print(f'@{k} {met} delta={sum(d)/len(d):+.4f} 95%CI[{bs[50]:+.4f},{bs[1949]:+.4f}] better={sum(x>0 for x in d)} worse={sum(x<0 for x in d)}')
routed=Counter(); cost=0
for l in open(V+'esci-pool-judge-20261023-jev/judgments.jsonl'):
    u=json.loads(l)['usage']; routed[u.get('routed_model')]+=1; cost+=u.get('cost') or 0
print('\nrouted models',dict(routed),'total cost USD',round(cost,4))
json.dump(out,open(V+'esci-pool-judge-20261023-jev/rescore.json','w'),indent=1)
