"""Collect licensed organization directories; never infer caller identity or spam status."""
import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

BASE = 'https://www.data.go.kr'
SCHEMA_VERSION = 2
MAX_BYTES = 90_000_000


def request(url, data=None):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data is not None else None,
                                         headers={'User-Agent': 'JeonhwaHannun-DataCollector/0.2'})
            with urllib.request.urlopen(req, timeout=180) as response:
                result = response.read(MAX_BYTES + 1)
                if len(result) > MAX_BYTES:
                    raise ValueError('Response exceeds reviewed limit')
                return result
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def download(source):
    html = request(source['page']).decode('utf-8')
    metadata = [json.loads(v) for v in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)]
    metadata = next((d for d in metadata if d.get('@type') == 'Dataset'), None)
    if not metadata or metadata.get('license', {}).get('name') != source['license']:
        raise ValueError('License changed or missing')
    dates = re.findall(r'(\d{8})$', metadata.get('alternateName', ''))
    if not dates:
        raise ValueError('Dataset reference date missing')
    as_of = datetime.strptime(dates[0], '%Y%m%d').date().isoformat()
    match = re.search(r"fn_fileDataDown\('([^']+)',\s*'([^']+)',\s*'([^']*)',\s*'([^']+)'", html)
    if not match:
        raise ValueError('Official download metadata unavailable')
    pk, detail, attachment, serial = match.groups()
    info = json.loads(request(BASE + '/tcs/dss/selectFileDataDownload.do', {
        'publicDataPk': pk, 'publicDataDetailPk': detail, 'atchFileId': attachment,
        'fileDetailSn': serial, 'publicDataTyCode': 'PR0051'}))
    if info.get('status') is not True:
        raise ValueError('Download not authorized')
    params = {'atchFileId': info['atchFileId'], 'fileDetailSn': info['fileDetailSn']}
    limit = json.loads(request(BASE + '/cmm/cmm/check-limit.json', params))
    if limit.get('needCaptcha') is not False:
        raise ValueError('Human interaction required; no bypass')
    raw = request(BASE + '/cmm/cmm/fileDownload.do?' + urllib.parse.urlencode(params))
    return raw, {**source, 'data_as_of': as_of, 'metadata_modified': metadata.get('dateModified')}


def normalize(value):
    value = str(value or '').strip()
    if not re.fullmatch(r'\+?[0-9()\s-]+', value):
        return None
    digits = re.sub(r'\D', '', value)
    if value.startswith('+82'):
        digits = '0' + digits[2:]
    if not re.fullmatch(r'(?:02\d{7,8}|0(?:3[1-3]|4[1-4]|5[1-5]|6[1-4])\d{7}|070\d{8}|080\d{7}|1[568]\d{6})', digits):
        return None
    # Clearly placeholder subscriber values must not become a shared representative number.
    local = digits[2:] if digits.startswith('02') else digits[3:]
    if len(set(local)) == 1:
        return None
    return digits


def read_csv(raw):
    for encoding in ('utf-8-sig', 'cp949'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    else:
        raise ValueError('Unsupported encoding')
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError('Empty CSV')
    reader.fieldnames = [h.strip() for h in reader.fieldnames]
    yield from reader


def tables(raw, source):
    if source.get('format') != 'hira_zip':
        return [('directory', raw, source['category'])]
    archive = zipfile.ZipFile(io.BytesIO(raw))
    result = []
    for prefix, category in [('1.병원정보서비스', 'hospital'), ('2.약국정보서비스', 'pharmacy')]:
        matches = [i for i in archive.infolist() if i.filename.startswith(prefix) and i.filename.endswith('.csv')]
        if len(matches) != 1 or matches[0].file_size > MAX_BYTES:
            raise ValueError('HIRA archive schema changed')
        result.append((matches[0].filename, archive.read(matches[0]), category))
    return result


def extract(raw, source, today=None):
    today = today or date.today()
    age = (today - date.fromisoformat(source['data_as_of'])).days
    if age < 0:
        raise ValueError('Dataset reference date is in future')
    observations, reasons, counts = [], Counter(), Counter()
    seen = set()
    for table, data, category in tables(raw, source):
        rows = list(read_csv(data))
        if not rows or not set(source['fields'].values()).issubset(rows[0]):
            raise ValueError('CSV fields changed')
        counts[category] += len(rows)
        for row in rows:
            values = {k: str(row.get(v) or '').strip() for k, v in source['fields'].items()}
            number = normalize(values['phone'])
            if not number or not values['name'] or not values['address']:
                reasons['missing_or_invalid_fields'] += 1
                continue
            if any(x in values['name'] for x in ['운영중지', '폐국', '폐업', '휴업']):
                reasons['inactive_marker'] += 1
                continue
            key = number, values['name'], values['address']
            if key in seen:
                reasons['duplicate_rows'] += 1
                continue
            seen.add(key)
            observations.append({'phone': number, 'name': values['name'], 'address': values['address'],
                                 'category': category, 'source_id': source['id'], 'source_url': source['page'],
                                 'provider': source['provider'], 'license': source['license'],
                                 'data_as_of': source['data_as_of'], 'table': table,
                                 'expires_on': (date.fromisoformat(source['data_as_of']) + timedelta(days=source.get('max_age_days',365))).isoformat(),
                                 'stale': age > source.get('max_age_days', 365)})
    total = sum(counts.values())
    if len(observations) < total * source.get('min_valid_ratio',0.8):
        raise ValueError('Accepted row ratio dropped below reviewed threshold')
    if not source['min_rows'] <= total <= source['max_rows']:
        raise ValueError('Row count outside reviewed bounds')
    report = {'source_id': source['id'], 'input_rows': total, 'accepted_entity_rows': len(observations),
              'unique_numbers': len({o['phone'] for o in observations}), 'rows_by_category': dict(counts),
              'excluded': dict(reasons), 'data_as_of': source['data_as_of'],
              'stale_source': age > source.get('max_age_days', 365), 'sha256': hashlib.sha256(raw).hexdigest()}
    return observations, report


def canonical(value):
    return re.sub(r'[\s,()·]', '', value).casefold()


def reconcile(observations):
    groups = defaultdict(list)
    for item in observations:
        groups[item['phone']].append(item)
    result = []
    for phone, all_items in sorted(groups.items()):
        fresh = [o for o in all_items if not o['stale']]
        current = fresh or all_items
        entities = {(canonical(o['name']), canonical(o['address'])) for o in current}
        conflict = len(entities) > 1
        status = 'directory' if fresh and not conflict else 'review_required'
        selected = max(current, key=lambda o: o['data_as_of'])
        result.append({'phone': phone, 'name': selected['name'] if not conflict else '여러 기관 공유·충돌 번호',
                       'address': selected['address'] if not conflict else '', 'category': selected['category'],
                       'status': status, 'data_as_of': selected['data_as_of'], 'expires_on': selected['expires_on'],
                       'source_url': selected['source_url'], 'provider': selected['provider'],
                       'flags': (['shared_or_conflicting_number'] if conflict else []) + ([] if fresh else ['stale_source']),
                       'source_count': len({o['source_id'] for o in current}),
                       'observations': all_items, 'caller_authenticated': False})
    return result


def write_db(path, records):
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript('''PRAGMA user_version=2;
        CREATE TABLE directory(phone TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL,
          category TEXT NOT NULL, data_as_of TEXT NOT NULL, source_url TEXT NOT NULL,
          provider TEXT NOT NULL, source_count INTEGER NOT NULL, expires_on TEXT NOT NULL);
        CREATE INDEX directory_name ON directory(name);
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        db.executemany('INSERT INTO directory VALUES(?,?,?,?,?,?,?,?,?)',
                       [(r['phone'],r['name'],r['address'],r['category'],r['data_as_of'],r['source_url'],r['provider'],r['source_count'],r['expires_on'])
                        for r in records if r['status'] == 'directory'])
        db.executemany('INSERT INTO metadata VALUES(?,?)', [('schema_version','2'),('meaning','Public directory association; caller identity not verified')])
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
    db.close()
    with sqlite3.connect(path) as verify:
        expected=sum(r['status']=='directory' for r in records)
        if verify.execute('SELECT COUNT(*) FROM directory').fetchone()[0] != expected:
            raise ValueError('Persisted SQLite row count mismatch')


def build(records, reports, out, previous=None):
    eligible = [r for r in records if r['status'] == 'directory']
    if not eligible:
        raise ValueError('No publishable directory associations')
    current = {r['phone']: hashlib.sha256(json.dumps({k:r[k] for k in ['name','address','category','data_as_of','provider','source_url','expires_on']},sort_keys=True).encode()).hexdigest() for r in eligible}
    previous = previous or {}
    if previous and len(current) < len(previous)*0.8:
        raise ValueError('Directory shrank by more than 20 percent; retain previous release')
    changes = {'baseline': not bool(previous), 'added': sorted(current.keys()-previous.keys()),
               'removed': sorted(previous.keys()-current.keys()),
               'changed': sorted(k for k in current.keys() & previous.keys() if current[k] != previous[k])}
    write_db(out / 'phone-db.sqlite', records)
    (out/'index.json').write_text(json.dumps(current,sort_keys=True))
    (out/'changes.json').write_text(json.dumps(changes,ensure_ascii=False))
    (out/'candidates.json.gz').write_bytes(gzip.compress(json.dumps(records,ensure_ascii=False).encode(),mtime=0))
    cross=[]
    for r in records:
        if len({o['source_id'] for o in r['observations']})>1:
            pairs={(canonical(o['name']),canonical(o['address'])) for o in r['observations']}
            cross.append({'phone':r['phone'],'agreement':len(pairs)==1,'source_ids':sorted({o['source_id'] for o in r['observations']}),'current_status':r['status']})
    (out/'cross-source-review.json').write_text(json.dumps(cross,ensure_ascii=False))
    (out/'sources.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))
    digest = hashlib.sha256(json.dumps({'schema':SCHEMA_VERSION,'pipeline_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'reports':reports,'index':current},sort_keys=True).encode()).hexdigest()
    manifest = {'schema_version':SCHEMA_VERSION,'version':digest[:20], 'directory_numbers':len(eligible),
                'review_numbers':len(records)-len(eligible), 'multi_source_numbers':sum(r['source_count']>1 for r in eligible),
                'caller_authenticated':False, 'sources':reports,
                'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir()) if p.is_file() and p.name not in ['manifest.json','manifest-public.der','manifest.sig']}}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    # Snapshot key is discarded after signing. Its public half is pinned inside the signed APK.
    with tempfile.TemporaryDirectory(prefix='phone-sign-') as private_dir:
        key=Path(private_dir)/'key.pem'
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(key)],check=True,capture_output=True)
        key.chmod(0o600)
        subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-outform','DER','-out',str(out/'manifest-public.der')],check=True,capture_output=True)
        subprocess.run(['openssl','dgst','-sha256','-sign',str(key),'-out',str(out/'manifest.sig'),str(out/'manifest.json')],check=True,capture_output=True)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--offline-dir', type=Path)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--out', type=Path, default=Path('build'))
    args = parser.parse_args()
    args.out.mkdir(exist_ok=True, parents=True)
    records, reports, failures = [], [], []
    for source in json.loads(Path('config/sources.json').read_text()):
        if not source.get('enabled'):
            continue
        try:
            if args.offline_dir:
                raw = (args.offline_dir/source['id']).read_bytes()
            else:
                raw, source = download(source)
            rows, report = extract(raw,source)
            records.extend(rows)
            reports.append(report)
            (args.out/(source['id']+'.raw')).write_bytes(raw)
            print(json.dumps(report,ensure_ascii=False),flush=True)
        except Exception as exc:
            failures.append({'source_id':source['id'],'error_type':type(exc).__name__})
    summary = {'collected_at':datetime.now(timezone.utc).isoformat(),'sources':reports,'failures':failures}
    (args.out/'quality-report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    if failures:
        print(json.dumps(failures)); raise SystemExit(1)
    reconciled = reconcile(records)
    previous = json.loads(args.previous.read_text()) if args.previous and args.previous.exists() else None
    manifest = build(reconciled,reports,args.out,previous)
    print(json.dumps({k:v for k,v in manifest.items() if k not in ['files','sources']},ensure_ascii=False))

if __name__ == '__main__':
    main()
