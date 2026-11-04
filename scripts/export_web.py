import sqlite3,json,hashlib,shutil,datetime
from pathlib import Path

def export(build,dist):
    target=dist/'data';target.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(build/'phone-db.sqlite') as db:
        db.row_factory=sqlite3.Row
        rows=[dict(r) for r in db.execute('select * from directory order by phone')]
    if not rows: raise ValueError('Empty directory')
    manifest=json.loads((build/'manifest.json').read_text())
    version=manifest['version']
    folder=target/version;folder.mkdir(exist_ok=True)
    content=json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode()
    (folder/'directory.json').write_bytes(content)
    shutil.copyfile(build/'phone-db.sqlite',folder/'phone-db.sqlite')
    definitions={s['id']:s for s in json.loads((Path(__file__).resolve().parents[1]/'config/sources.json').read_text())}
    sources=[{**r,**{k:definitions.get(r['source_id'],{}).get(k,r.get(k,'')) for k in ['provider','title','page','license']}} for r in manifest['sources']]
    public={'version':version,'count':len(rows),'collected_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'schedule':'매주 월요일 06:23 KST','directory_sha256':hashlib.sha256(content).hexdigest(),'db_sha256':hashlib.sha256((folder/'phone-db.sqlite').read_bytes()).hexdigest(),'sources':sources}
    (target/'latest.json').write_text(json.dumps(public,ensure_ascii=False))
    shutil.copyfile(build/'index.json',target/'index.json')
    return public
if __name__=='__main__': export(Path('build'),Path('dist'))
