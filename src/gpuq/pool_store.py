"""Single-master pool metadata in SQLite, optionally accessed over persistent SSH.

The database stays on the master filesystem. Workers never replicate/fail over this
store. Transactions and lease expiry share one BEGIN IMMEDIATE serialization point.
SSH uses the existing authenticated profile and a bounded JSON-line RPC protocol.
"""
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time

from .coordination import Record, Unavailable

FLAG = '--gpuq-pool-store'
LIMIT = 16 * 1024 * 1024


class SQLiteStore:
    single_master = True

    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sequence (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL);
                INSERT OR IGNORE INTO sequence VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS leases (id INTEGER PRIMARY KEY AUTOINCREMENT, ttl REAL, expires REAL);
                CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT, revision INTEGER, lease INTEGER);
            ''')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            db.execute('DELETE FROM records WHERE lease IN (SELECT id FROM leases WHERE expires<=?)', (now,))
            # Keep lease IDs monotonic across expiry and process restarts.
            db.execute('DELETE FROM leases WHERE expires<=?', (now,))
            yield db
            db.commit()
        except sqlite3.Error as error:
            raise Unavailable('master SQLite unavailable') from error
        finally:
            db.close()

    def get(self, key):
        with self.transaction() as db:
            row = db.execute('SELECT value,revision FROM records WHERE key=?', (key,)).fetchone()
            return Record(json.loads(row[0]), row[1]) if row else None

    def scan(self, prefix):
        with self.transaction() as db:
            rows = db.execute('SELECT key,value,revision FROM records WHERE substr(key,1,?)=?',
                              (len(prefix), prefix)).fetchall()
            return {key: Record(json.loads(value), rev) for key, value, rev in rows}

    def txn(self, checks, puts, *, deletes=(), leases=None):
        with self.transaction() as db:
            for key, expected in checks.items():
                row = db.execute('SELECT revision FROM records WHERE key=?', (key,)).fetchone()
                if (row[0] if row else 0) != (expected.revision if expected else 0):
                    return False
            for lease in (leases or {}).values():
                if db.execute('SELECT id FROM leases WHERE id=?', (lease,)).fetchone() is None:
                    raise Unavailable('pool lease expired')
            db.execute('UPDATE sequence SET value=value+1 WHERE id=1')
            revision = db.execute('SELECT value FROM sequence WHERE id=1').fetchone()[0]
            for key, value in puts.items():
                encoded = json.dumps(value)
                if len(encoded.encode()) > 1024 * 1024:
                    raise ValueError('pool record exceeds 1 MiB')
                db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)',
                           (key, encoded, revision, (leases or {}).get(key)))
            for key in deletes:
                db.execute('DELETE FROM records WHERE key=?', (key,))
            return True

    def grant(self, ttl):
        if not 0 < ttl <= 86400:
            raise ValueError('invalid lease duration')
        with self.transaction() as db:
            return db.execute('INSERT INTO leases(ttl,expires) VALUES (?,?)',
                              (ttl, self.clock() + ttl)).lastrowid

    def keepalive(self, lease):
        with self.transaction() as db:
            if not db.execute('UPDATE leases SET expires=?+ttl WHERE id=?', (self.clock(), lease)).rowcount:
                raise Unavailable('pool lease expired')

    def members(self):
        return 1


class SSHStore:
    """Persistent SSH channel to the sole SQLite master; no retry after ambiguous RPC.

    Each request has a bounded size and deadline. Broken channels are closed; the
    next explicit operation may reconnect, but the failed operation is never replayed.
    """
    single_master = True

    def __init__(self, profile, namespace, *, timeout=15):
        self.profile, self.namespace, self.timeout = profile, namespace, timeout
        self.process = None

    def close(self):
        if self.process:
            self.process.stdin.close()
            self.process.stdout.close()
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None

    def rpc(self, method, **arguments):
        from .remote import ssh_command
        payload = json.dumps({'method': method, 'arguments': arguments}).encode() + b'\n'
        if len(payload) > LIMIT:
            raise ValueError('pool request exceeds 16 MiB')
        try:
            if self.process is None:
                command = ssh_command(self.profile)
                command[-1] = command[-1].replace('--gpuq-remote-call', FLAG)
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
                os.set_blocking(self.process.stdin.fileno(), False)
                os.set_blocking(self.process.stdout.fileno(), False)
                header = json.dumps({'config': self.profile.get('config'), 'namespace': self.namespace}).encode()
                payload = header + b'\n' + payload
            deadline = time.monotonic() + self.timeout
            sent, reply = 0, bytearray()
            while b'\n' not in reply:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('pool RPC timed out')
                readable, writable, _ = select.select(
                    [self.process.stdout], [self.process.stdin] if sent < len(payload) else [], [], remaining)
                if writable:
                    sent += os.write(self.process.stdin.fileno(), payload[sent:sent + 65536])
                if readable:
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        raise Unavailable('master SSH channel closed')
                    reply.extend(chunk)
                    if len(reply) > LIMIT:
                        raise ValueError('pool response exceeds 16 MiB')
            result = json.loads(reply)
            if 'error' in result:
                raise Unavailable(result['error'])
            return result['result']
        except (OSError, ValueError, KeyError, Unavailable) as error:
            self.close()
            raise Unavailable('master RPC failed; inspect queue before retrying') from error

    def get(self, key):
        result = self.rpc('get', key=key)
        return Record(**result) if result else None

    def scan(self, prefix):
        return {key: Record(**value) for key, value in self.rpc('scan', prefix=prefix).items()}

    def txn(self, checks, puts, *, deletes=(), leases=None):
        return self.rpc('txn', checks={k: asdict(v) if v else None for k, v in checks.items()},
                        puts=puts, deletes=deletes, leases=leases)

    def grant(self, ttl):
        return self.rpc('grant', ttl=ttl)

    def keepalive(self, lease):
        return self.rpc('keepalive', lease=lease)

    def members(self):
        return self.rpc('members')


def serve():
    """Serve whitelisted store operations using the master config, never a caller DB path."""
    from .cluster import load_cluster
    from .config import load_settings
    header = sys.stdin.buffer.readline(LIMIT + 1)
    if len(header) > LIMIT or not header.endswith(b'\n'):
        raise ValueError('invalid pool header')
    request = json.loads(header)
    config = load_cluster(load_settings(config_path=request.get('config')))
    if config.backend != 'sqlite' or request.get('namespace') != config.namespace:
        raise ValueError('pool RPC requires the matching SQLite master configuration')
    store = config.store()
    while True:
        line = sys.stdin.buffer.readline(LIMIT + 1)
        if not line:
            return 0
        if len(line) > LIMIT or not line.endswith(b'\n'):
            raise ValueError('invalid pool request')
        try:
            request = json.loads(line)
            method, args = request['method'], request.get('arguments', {})
            if method not in {'get', 'scan', 'txn', 'grant', 'keepalive', 'members'}:
                raise ValueError('unsupported pool operation')
            if method == 'txn':
                args['checks'] = {key: Record(**value) if value else None for key, value in args['checks'].items()}
            result = getattr(store, method)(**args)
            if method == 'get' and result is not None:
                result = asdict(result)
            if method == 'scan':
                result = {key: asdict(value) for key, value in result.items()}
            response = json.dumps({'result': result})
            if len(response.encode()) > LIMIT - 1:
                raise ValueError('pool response exceeds 16 MiB')
        except (ValueError, KeyError, TypeError, Unavailable):
            response = json.dumps({'error': 'pool operation failed'})
        print(response, flush=True)
