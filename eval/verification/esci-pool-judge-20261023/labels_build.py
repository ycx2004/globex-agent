import json,pandas as pd
cases={int(json.loads(l)['id'].split('-')[-1]) for l in open('/w/product_recall_esci.jsonl')}
amap=json.load(open('/w/asin_map.json'))
ex=pd.read_parquet('/raw/shopping_queries_dataset_examples.parquet',columns=['query_id','product_id','esci_label','product_locale'])
ex=ex[(ex.product_locale=='us')&ex.query_id.isin(cases)]
out={}
for q,p,l in zip(ex.query_id,ex.product_id,ex.esci_label):
    if p in amap: out.setdefault(f'esci-us-{q}',{})[amap[p]]=l
json.dump(out,open('/w/labels.json','w'))
print(len(out))
