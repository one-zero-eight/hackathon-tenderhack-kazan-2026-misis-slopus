import asyncio
import fcntl
import gzip
import hashlib
import json
import signal
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data' / 'ste.jsonl.gz'
DATABASE = ROOT / 'data' / 'tmp' / 'ste-details.sqlite'
ENDPOINT = 'https://api.zakupki.mos.ru/api/v2/eetp/sku/GetDetails'
COOLDOWN_SECONDS = 30 * 60
PAUSE_SECONDS = 10
BATCH_SIZE = 100
MAX_RUN_SECONDS = 6 * 60 * 60
STOP_REQUESTED = False


def stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def iter_records(source: Path):
    opener = gzip.open if source.suffix == '.gz' else open
    with opener(source, 'rt', encoding='utf-8') as stream:
        if source.name.removesuffix('.gz').endswith('.jsonl'):
            for line in stream:
                if line.strip():
                    yield json.loads(line)
        else:
            value = json.load(stream)
            yield from value['items'] if isinstance(value, dict) else value


def now():
    return datetime.now(timezone.utc).isoformat()


def fetch(ids, token):
    query = {'filter': {'id': {'values': ids}}, 'take': len(ids), 'skip': 0, 'withCount': True}
    url = ENDPOINT + '?' + urllib.parse.quote(json.dumps(query, separators=(',', ':')))
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def update_metadata(db, **fields):
    db.executemany('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', [(key, json.dumps(value, ensure_ascii=False)) for key, value in fields.items()])


async def wait(seconds):
    deadline = time.monotonic() + seconds
    while not STOP_REQUESTED and time.monotonic() < deadline:
        await asyncio.sleep(min(5, max(0, deadline - time.monotonic())))


def load_token() -> str:
    token = (ROOT / 'secrets' / 'zakupki-token').read_text(encoding='utf-8').strip()
    if token.lower().startswith('bearer '):
        token = token[7:]
    if not token or any(c.isspace() for c in token):
        raise RuntimeError('Invalid token file; value not logged')
    return token


def export_details(db, export: Path, completed: int):
    export_metadata = {
        key: json.loads(value)
        for key, value in db.execute('SELECT key,value FROM metadata')
    }
    export_metadata['format'] = 'jsonl'
    export_metadata['exportedAt'] = now()
    use_gzip = export.suffix == '.gz' or export.name.endswith('.jsonl.gz')
    if use_gzip:
        export_metadata['compression'] = 'gzip'

    temporary = export.with_name(export.name + '.part')
    opener = gzip.open if use_gzip else open
    with opener(temporary, 'wt', encoding='utf-8') as stream:
        for (payload,) in db.execute('SELECT d.payload FROM sku_details d JOIN targets t USING(sku_id) ORDER BY t.ordinal'):
            stream.write(payload + '\n')
    temporary.replace(export)
    Path(str(export) + '.meta.json').write_text(json.dumps(export_metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'EXPORTED: {export.name}; {completed} cards; bytes={export.stat().st_size}', flush=True)


async def download_details(
    ids: list[int],
    *,
    database: Path,
    export: Path,
    list_source: str | Path,
    token: str,
    cooldown_seconds: int = 0,
):
    target = len(ids)
    if cooldown_seconds:
        scheduled = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
        print(f'SCHEDULED: batch details probe at {scheduled.isoformat()} UTC; waiting {cooldown_seconds}s; no API requests during cooldown', flush=True)
        await wait(cooldown_seconds)
        if STOP_REQUESTED:
            print('Stopped during cooldown; no API requests made', flush=True)
            return
    fingerprint = hashlib.sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest()
    db = sqlite3.connect(database)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS targets(ordinal INTEGER NOT NULL UNIQUE, sku_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS sku_details(
            sku_id INTEGER PRIMARY KEY REFERENCES targets(sku_id),
            fetched_at TEXT NOT NULL,
            payload TEXT NOT NULL CHECK(json_valid(payload))
        );
    ''')
    prior = db.execute('SELECT value FROM metadata WHERE key=?', ('listFingerprint',)).fetchone()
    if prior and json.loads(prior[0]) != fingerprint:
        db.close()
        raise RuntimeError('Saved details belong to a different SKU list')
    list_source = str(list_source)
    with db:
        if not prior:
            db.executemany('INSERT INTO targets(ordinal,sku_id) VALUES(?,?)', enumerate(ids))
            update_metadata(db, listFingerprint=fingerprint, source=ENDPOINT, listSource=list_source, targetCount=target, startedAt=now())
        else:
            saved_source = db.execute('SELECT value FROM metadata WHERE key=?', ('source',)).fetchone()
            if not saved_source or json.loads(saved_source[0]) != ENDPOINT:
                raise RuntimeError('Existing database uses a different details API')
        update_metadata(db, listSource=list_source, pauseSeconds=PAUSE_SECONDS, batchSize=BATCH_SIZE, stopReason='in_progress', resumeStartedAt=now(), finishedAt=None)
    completed = db.execute('SELECT count(*) FROM sku_details').fetchone()[0]
    pending = [row[0] for row in db.execute('SELECT t.sku_id FROM targets t LEFT JOIN sku_details d USING(sku_id) WHERE d.sku_id IS NULL ORDER BY t.ordinal')]
    prefix = 'COOLDOWN COMPLETE' if cooldown_seconds else 'READY'
    print(f'{prefix}: {completed} saved; {len(pending)} pending. First request limited to 2 SKUs', flush=True)
    reason = 'complete'
    requests = 0
    deadline = time.monotonic() + MAX_RUN_SECONDS
    offset = 0
    try:
        while offset < len(pending):
            if STOP_REQUESTED:
                reason = 'stop_requested'
                break
            if time.monotonic() + PAUSE_SECONDS + 60 >= deadline:
                reason = 'time_limit'
                break
            if requests:
                await wait(PAUSE_SECONDS)
            if STOP_REQUESTED:
                reason = 'stop_requested'
                break
            batch_ids = pending[offset:offset + (2 if requests == 0 else BATCH_SIZE)]
            requests += 1
            try:
                result = await asyncio.to_thread(fetch, batch_ids, token)
            except urllib.error.HTTPError as error:
                reason = f'http_{error.code}'
                print(f'STOP: HTTP {error.code}; Retry-After={error.headers.get("Retry-After")}; no retries or alternate endpoints', flush=True)
                with db:
                    update_metadata(db, failedSkuIds=batch_ids, lastHttpStatus=error.code, retryAfter=error.headers.get('Retry-After'))
                break
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
                reason = 'network_error'
                print('STOP: network error; no retries', flush=True)
                break
            cards = result.get('items') if isinstance(result, dict) else None
            if not isinstance(cards, list) or len(cards) != len(batch_ids) or result.get('count') != len(batch_ids):
                reason = 'incomplete_batch'
                print('STOP: batch response incomplete or unexpected; no retries', flush=True)
                break
            if not all(isinstance(card, dict) and isinstance(card.get('name'), str) and isinstance(card.get('skuCharacteristics'), list) for card in cards):
                reason = 'invalid_response'
                print('STOP: detailed-card response structure invalid', flush=True)
                break
            returned_ids = [card.get('id') for card in cards]
            if len(set(returned_ids)) != len(batch_ids) or set(returned_ids) != set(batch_ids):
                reason = 'unexpected_ids'
                print('STOP: batch returned unexpected or duplicate SKU IDs', flush=True)
                break
            payloads = [json.dumps(card, ensure_ascii=False, separators=(',', ':')) for card in cards]
            if any(token in payload for payload in payloads):
                reason = 'credential_in_response'
                break
            with db:
                fetched_at = now()
                db.executemany('INSERT INTO sku_details(sku_id,fetched_at,payload) VALUES(?,?,?)', [(card['id'], fetched_at, payload) for card, payload in zip(cards, payloads)])
                completed += len(cards)
                update_metadata(db, recordCount=completed, checkpointAt=fetched_at)
            offset += len(batch_ids)
            print(f'CHECKPOINT: request={requests}; +{len(cards)} full cards; {completed}/{target}', flush=True)
    except BaseException:
        reason = 'interrupted_or_error'
        raise
    finally:
        with db:
            update_metadata(db, recordCount=completed, stopReason=reason, finishedAt=now(), lastRunRequestCount=requests)
        valid = db.execute('SELECT count(*) FROM sku_details WHERE json_valid(payload) AND json_extract(payload,\'$.id\')=sku_id').fetchone()[0]
        if valid != completed:
            raise RuntimeError('Stored cards failed validation')
        if reason == 'complete' and completed != target:
            raise RuntimeError('Missing full cards at completion')
        if completed:
            export_details(db, export, completed)
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.close()
        print(f'FINISHED: {database.name}; {completed} validated full cards; stop={reason}', flush=True)


def acquire_locks():
    run_lock = open('/tmp/slider-ste-details.lock', 'a')
    fcntl.flock(run_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    api_lock = open('/tmp/slider-ste-download.lock', 'a')
    fcntl.flock(api_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return run_lock, api_lock


async def main():
    acquire_locks()
    ids = [item['id'] for item in iter_records(SOURCE)]
    metadata = json.loads(Path(str(SOURCE) + '.meta.json').read_text(encoding='utf-8'))
    target = len(ids)
    if metadata['source'] != 'https://api.zakupki.mos.ru/api/v2/Sku/public/Search' or metadata['recordCount'] != target or target != 10000:
        raise RuntimeError('Unexpected list checkpoint; no API requests made')
    if len(set(ids)) != target or any(a >= b for a, b in zip(ids, ids[1:])):
        raise RuntimeError('List validation failed; no API requests made')
    await download_details(
        ids,
        database=DATABASE,
        export=ROOT / 'data' / 'ste-details.jsonl.gz',
        list_source=SOURCE,
        token=load_token(),
        cooldown_seconds=COOLDOWN_SECONDS,
    )


if __name__ == '__main__':
    asyncio.run(main())
