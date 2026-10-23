"""用人工 ESCI 标签 + LLM 池化补判重算指标；人工标签优先。"""
import json, math, sys
V='/root/apps/globex-agent/eval/verification/'
H=json.load(open('/tmp/bc/labels.json'))
J={}
for f in ('esci-pool-judge-20261023','esci-pool-judge-20261023-extra-qu'):
    for l in open(V+f+'/judgments.jsonl'):
        d=json.loads(l); J.setdefault(d['id'],{}).update({p:v for p,v in d['judged'].items() if v})
GAIN={'E':1,'S':0.1,'C':0.01,'I':0}
def lab(q,p,aug): 
    h=H.get(q,{}).get(p)
    return h if h or not aug else J.get(q,{}).get(p)
def metrics(rows,k,aug):
    agg={}
    for r in rows:
        q=r['id']; ret=r['retrieved'][:k]; L=[lab(q,p,aug) for p in ret]
        Eall={p for p,v in H.get(q,{}).items() if v=='E'}|({p for p,v in J.get(q,{}).items() if v=='E' and p not in H.get(q,{})} if aug else set())
        e=[x=='E' for x in L]
        m={'hit_E':any(e),'hit_ES':any(x in('E','S') for x in L),'P@k_E':sum(e)/k,
           'recall_capped':sum(e)/min(len(Eall),k) if Eall else 0,
           'mrr':next((1/(i+1) for i,x in enumerate(e) if x),0),
           'ndcg_esci':0,'unjudged':sum(x is None for x in L)/k}
        dcg=sum(GAIN.get(x,0)/math.log2(i+2) for i,x in enumerate(L))
        allg=sorted([GAIN[v] for v in H.get(q,{}).values()]+([GAIN[v] for p,v in J.get(q,{}).items() if p not in H.get(q,{})] if aug else []),reverse=True)[:k]
        idcg=sum(g/math.log2(i+2) for i,g in enumerate(allg)); m['ndcg_esci']=dcg/idcg if idcg else 0
        for a,b in m.items(): agg[a]=agg.get(a,0)+b
    return {a:round(b/len(rows),3) for a,b in agg.items()}
out={}
for name,d,k in [('baseline@8','product-recall-20261023-hybrid12-c64-k8',8),('qu@8','product-recall-20261023-query-understanding-c64-k8',8),
                 ('baseline@16','product-recall-20261023-hybrid12-c64-k16',16),('qu@16','product-recall-20261023-query-understanding-c64-k16',16)]:
    for s in ('hybrid_only','hybrid_rerank'):
        rows=[json.loads(l) for l in open(V+d+f'/per_query_{s}.jsonl')]
        for aug in (False,True):
            key=f"{name} {s} {'pooled' if aug else 'human'}"; out[key]=metrics(rows,k,aug); print(key.ljust(42),out[key])
json.dump(out,open(V+'esci-pool-judge-20261023/rescore.json','w'),indent=1)
# 标签分布与校准
c=a=0;dist={}
for l in open(V+'esci-pool-judge-20261023/judgments.jsonl'):
    d=json.loads(l)
    for v in d['judged'].values(): dist[v]=dist.get(v,0)+1
    for v in d['calibration'].values(): c+=1; a+=v['human']==v['llm']
print('LLM label dist',dist,'calibration agree',a,c,round(a/c,3))
