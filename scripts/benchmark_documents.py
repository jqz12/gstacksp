"""Zero-model synthetic document scale check; never touches real task history."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gsp.documents import Governance
from gsp.runtime import atomic, snapshot


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--sizes',type=int,nargs='+',default=[100,1000,3000,10000]);a=p.parse_args()
    rows=[]
    with tempfile.TemporaryDirectory(prefix='gsp-document-scale-') as tmp:
        root=Path(tmp);book=Governance(root);module=book.module('Authentication',aliases=['login'])
        (root/'app.py').write_text('print("fixture")\n',encoding='utf-8')
        created=0
        for size in sorted(a.sizes):
            for n in range(created+1,size+1):
                folder=f'docs/work/2026/{((n-1)%12)+1:02d}/GSP-{n:06d}-fixture';path=root/folder;path.mkdir(parents=True)
                record={'schema':1,'kind':'work','uuid':uuid.uuid5(uuid.NAMESPACE_URL,'gsp-scale-'+str(n)).hex,'number':f'GSP-{n:06d}',
                        'title':f'Task {n}','summary':'Historical account constraint','source':'synthetic fixture','stage':'closed',
                        'folder':folder,'documents':{'brief':folder+'/brief.md'},'modules':[module['uuid']],'relations':[],
                        'run_id':None,'generation':1,'created':'2026-01-01','updated':'2026-01-01','history':[]}
                (path/'record.json').write_text(json.dumps(record),encoding='utf-8');(path/'brief.md').write_text('# Historical account constraint\n\n'+'fixture '*100,encoding='utf-8')
            created=size;atomic(root/'.gsp/work-counter.json',{'next':size+1})
            start=time.perf_counter();book.rebuild();rebuild=time.perf_counter()-start
            start=time.perf_counter();hits=book.search(module=module['uuid']);query=time.perf_counter()-start
            start=time.perf_counter();fresh=book.create('New task at '+str(size),'New brief','benchmark',modules=[module['uuid']]);create=time.perf_counter()-start
            start=time.perf_counter();book.update(fresh['uuid'],summary='Changed summary');update=time.perf_counter()-start
            start=time.perf_counter();inputs=snapshot(root);fingerprint=time.perf_counter()-start
            assert len(hits['results'])<=10 and not any(k.startswith('docs/work/') for k in inputs)
            rows.append({'historical_work_items':size,'rebuild_s':round(rebuild,4),'query_s':round(query,4),'create_s':round(create,4),
                         'update_s':round(update,4),'snapshot_s':round(fingerprint,4),'query_results':len(hits['results']),
                         'result_characters':len(json.dumps(hits,ensure_ascii=False)),'snapshot_entries':len(inputs),'sqlite_bytes':book.index.stat().st_size})
            # Remove only this synthetic new item before expanding numbered fixture.
            folder=root/fresh['folder']
            for f in folder.iterdir():f.unlink()
            folder.rmdir()
    result={'at':datetime.now().astimezone().isoformat(),'python':sys.version,'platform':platform.platform(),'rows':rows,
            'limits':'Single samples, warm local cache; not p95, not real host/model validation. Text count is characters, not tokens. Fixture construction excluded.'}
    atomic(Path(a.output),result);print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
