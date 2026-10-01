"""Append-only source snapshots; conservative point-in-time visibility."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))

def now():
    return datetime.now(timezone.utc).isoformat()

def instant(value):
    d = datetime.fromisoformat(value)
    if d.tzinfo is None:
        d = d.replace(tzinfo=JST)
    return d.astimezone(timezone.utc).isoformat()

def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def st(value):
    if value is None:
        return None
    s = str(value).strip()
    return -abs(number(s[1:])) if s.startswith('F') and number(s[1:]) is not None else number(s)

def raceid(value):
    value = str(value).replace('-', '')
    if not re.fullmatch(r'\d{12}', value) or not 1 <= int(value[8:10]) <= 24 or not 1 <= int(value[10:]) <= 12:
        raise ValueError('invalid race ID: ' + value)
    datetime.strptime(value[:8], '%Y%m%d')
    return value

SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS raw_objects(
 sha TEXT PRIMARY KEY, path TEXT NOT NULL, bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fetches(
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL,
 observed_at TEXT NOT NULL, status INTEGER, sha TEXT REFERENCES raw_objects,
 error TEXT, normalized INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS races(
 race_id TEXT PRIMARY KEY, race_date TEXT NOT NULL, venue INTEGER NOT NULL, race_number INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(
 id INTEGER PRIMARY KEY, race_id TEXT NOT NULL REFERENCES races, kind TEXT NOT NULL,
 source TEXT NOT NULL, source_at TEXT, first_seen_at TEXT NOT NULL,
 payload TEXT NOT NULL, fingerprint TEXT NOT NULL, fetch_id INTEGER REFERENCES fetches,
 UNIQUE(race_id,kind,source,fingerprint));
CREATE INDEX IF NOT EXISTS snapshots_lookup ON snapshots(race_id,kind,first_seen_at);
CREATE TABLE IF NOT EXISTS auxiliary(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL,
 entity_key TEXT NOT NULL, first_seen_at TEXT NOT NULL, source_at TEXT,
 payload TEXT NOT NULL, fingerprint TEXT NOT NULL, fetch_id INTEGER REFERENCES fetches,
 UNIQUE(kind,source,entity_key,fingerprint));
CREATE TABLE IF NOT EXISTS profile_builds(
 id INTEGER PRIMARY KEY, as_of TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS predictions(
 id TEXT PRIMARY KEY, race_id TEXT NOT NULL REFERENCES races, predicted_at TEXT NOT NULL,
 model_version TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(
 id INTEGER PRIMARY KEY, prediction_id TEXT NOT NULL REFERENCES predictions,
 reviewed_at TEXT NOT NULL, result_fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(prediction_id,result_fingerprint));
CREATE TRIGGER IF NOT EXISTS prediction_no_update BEFORE UPDATE ON predictions BEGIN SELECT RAISE(ABORT,'predictions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS prediction_no_delete BEFORE DELETE ON predictions BEGIN SELECT RAISE(ABORT,'predictions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_no_update BEFORE UPDATE ON snapshots BEGIN SELECT RAISE(ABORT,'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_no_delete BEFORE DELETE ON snapshots BEGIN SELECT RAISE(ABORT,'snapshots are immutable'); END;
'''

class Store:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'boatrace.sqlite', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def record_fetch(self, source, url, observed, status, body=None, error=None):
        sha = None
        if body is not None:
            sha = hashlib.sha256(body).hexdigest()
            path = self.root / 'raw' / sha[:2] / sha
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                with path.open('xb') as f:
                    f.write(body)
            self.db.execute('INSERT OR IGNORE INTO raw_objects VALUES(?,?,?)', (sha, str(path.relative_to(self.root)), len(body)))
        cur = self.db.execute('INSERT INTO fetches(source,url,observed_at,status,sha,error) VALUES(?,?,?,?,?,?)',
                              (source, url, instant(observed), status, sha, error))
        self.db.commit()
        return cur.lastrowid

    def put(self, rid, kind, source, payload, observed, source_at=None, fetch_id=None):
        rid = raceid(rid)
        date = datetime.strptime(rid[:8], '%Y%m%d').date().isoformat()
        self.db.execute('INSERT OR IGNORE INTO races VALUES(?,?,?,?)', (rid, date, int(rid[8:10]), int(rid[10:])))
        value = dump(payload)
        fingerprint = hashlib.sha256((str(source_at) + value).encode()).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO snapshots(race_id,kind,source,source_at,first_seen_at,payload,fingerprint,fetch_id) VALUES(?,?,?,?,?,?,?,?)',
                        (rid, kind, source, instant(source_at) if source_at else None, instant(observed), value, fingerprint, fetch_id))

    def aux(self, kind, source, key, payload, observed, source_at=None, fetch_id=None):
        value = dump(payload)
        fp = hashlib.sha256((str(source_at)+value).encode()).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO auxiliary(kind,source,entity_key,first_seen_at,source_at,payload,fingerprint,fetch_id) VALUES(?,?,?,?,?,?,?,?)',
                        (kind,source,str(key),instant(observed),instant(source_at) if source_at else None,value,fp,fetch_id))

    def latest(self, rid, kind, as_of, source=None):
        sql = 'SELECT * FROM snapshots WHERE race_id=? AND kind=? AND first_seen_at<=? AND (source_at IS NULL OR source_at<=?)'
        params = [rid, kind, instant(as_of), instant(as_of)]
        if source:
            sql += ' AND source=?'
            params.append(source)
        rows = self.db.execute(sql + ' ORDER BY first_seen_at DESC,id DESC', params).fetchall()
        return rows[0] if rows else None

    def payload(self, rid, kind, as_of, source=None):
        row = self.latest(rid, kind, as_of, source)
        return json.loads(row['payload']) if row else None

    def backup(self, destination):
        self.db.commit()
        target = sqlite3.connect(destination)
        self.db.backup(target)
        target.close()
