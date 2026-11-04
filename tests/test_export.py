import unittest,tempfile,json,sys,hashlib
from pathlib import Path
from datetime import date
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from collect import extract,reconcile,build
from export_web import export
from test_collect import SOURCE
class PublicExportTests(unittest.TestCase):
    def test_review_and_raw_files_never_exported(self):
        rows,report=extract('약국명,전화번호,주소\nA,063-123-4567,주소A\nB,063-123-4568,주소B\nC,063-123-4568,주소C\n'.encode(),SOURCE,date(2026,10,31))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'build';out.mkdir();dist=root/'dist'
            build(reconcile(rows),[report],out);m=export(out,dist)
            public=dist/'data'/m['version'];content=(public/'directory.json').read_bytes()
            self.assertEqual([r['phone'] for r in json.loads(content)],['0631234567'])
            self.assertEqual(m['directory_sha256'],hashlib.sha256(content).hexdigest())
            self.assertEqual(m['db_sha256'],hashlib.sha256((public/'phone-db.sqlite').read_bytes()).hexdigest())
            self.assertEqual({p.name for p in (dist/'data').rglob('*') if p.is_file()},{'latest.json','index.json','directory.json','phone-db.sqlite'})
