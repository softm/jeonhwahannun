import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import normalize, extract, reconcile, build

SOURCE = dict(id='test', page='https://example.org', category='pharmacy',provider='기관',license='test',
              data_as_of='2026-09-30',min_rows=1,max_rows=10,fields={'name':'약국명','phone':'전화번호','address':'주소'})

class QualityTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(normalize('+82-63-564-2976'),'0635642976')
        self.assertEqual(normalize('1588-1234'),'15881234')
        for value in ['010-1234-5678','063-000-0000','063-123-4567 / 063-123-4568','','0631234567내선1']:
            self.assertIsNone(normalize(value))

    def test_collisions_staleness_and_exclusions(self):
        raw='약국명,전화번호,주소\nA,063-123-4567,주소A\nB,063-123-4567,주소B\nA,063-123-4567,주소A\nC,010-1234-5678,주소C\n'.encode()
        rows,report=extract(raw,{**SOURCE,'data_as_of':'2024-07-24','min_valid_ratio':0.4},date(2026,10,31))
        records=reconcile(rows)
        self.assertEqual(report['excluded']['duplicate_rows'],1)
        self.assertEqual(report['excluded']['missing_or_invalid_fields'],1)
        self.assertEqual(records[0]['status'],'review_required')
        self.assertEqual(set(records[0]['flags']),{'shared_or_conflicting_number','stale_source'})

    def test_fresh_source_supersedes_stale_and_preserves_provenance(self):
        raw='약국명,전화번호,주소\nA,063-123-4567,새주소\n'.encode()
        fresh,_=extract(raw,SOURCE,date(2026,10,31))
        stale={**fresh[0],'name':'옛기관','stale':True,'data_as_of':'2024-07-24','source_id':'old'}
        result=reconcile(fresh+[stale])[0]
        self.assertEqual(result['name'],'A')
        self.assertEqual(result['status'],'directory')
        self.assertEqual(len(result['observations']),2)

    def test_cross_source_agreement_and_disagreement(self):
        raw='약국명,전화번호,주소\nA,063-123-4567,주소A\n'.encode()
        rows,_=extract(raw,SOURCE,date(2026,10,31))
        other={**rows[0],'source_id':'other'}
        self.assertEqual(reconcile(rows+[other])[0]['source_count'],2)
        other['name']='B'
        self.assertEqual(reconcile(rows+[other])[0]['status'],'review_required')

    def test_db_delta_and_integrity(self):
        rows,report=extract('약국명,전화번호,주소\nA,063-123-4567,주소A\n'.encode(),SOURCE,date(2026,10,31))
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp); records=reconcile(rows); manifest=build(records,[report],out)
            with sqlite3.connect(out/'phone-db.sqlite') as db:
                self.assertEqual(db.execute('SELECT name FROM directory WHERE phone=?',('0631234567',)).fetchone()[0],'A')
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(manifest['files']['phone-db.sqlite'],hashlib.sha256((out/'phone-db.sqlite').read_bytes()).hexdigest())
            verification=['openssl','dgst','-sha256','-keyform','DER','-verify',str(out/'manifest-public.der'),'-signature',str(out/'manifest.sig'),str(out/'manifest.json')]
            self.assertEqual(subprocess.run(verification,capture_output=True).returncode,0)
            original=(out/'manifest.json').read_bytes()
            (out/'manifest.json').write_bytes(original+b' ')
            self.assertNotEqual(subprocess.run(verification,capture_output=True).returncode,0)
            (out/'manifest.json').write_bytes(original)
            index=json.loads((out/'index.json').read_text())
            records[0]['address']='새주소'; build(records,[report],out,index)
            self.assertEqual(json.loads((out/'changes.json').read_text())['changed'],['0631234567'])

    def test_schema_and_future_date_fail_closed(self):
        with self.assertRaises(ValueError):extract(b'<html>unavailable</html>',SOURCE,date(2026,10,31))
        with self.assertRaises(ValueError):extract(b'',{**SOURCE,'data_as_of':'2027-01-01'},date(2026,10,31))
