from __future__ import annotations
import json
import os
import threading
import psycopg2

DATABASE_URL = os.getenv('DATABASE_URL', '')
_lock = threading.RLock()

def _connect():
    if not DATABASE_URL:
        return None
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=8)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS autoclipper_jobs (job_id TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())")
    return conn

def _load(job_id):
    conn = _connect()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM autoclipper_jobs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()

def _save(job_id, payload):
    conn = _connect()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO autoclipper_jobs(job_id,payload,updated_at) VALUES(%s,%s::jsonb,NOW()) ON CONFLICT(job_id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()", (job_id, json.dumps(dict(payload))))
    finally:
        conn.close()

def _delete(job_id):
    conn = _connect()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM autoclipper_jobs WHERE job_id=%s", (job_id,))
    finally:
        conn.close()

class PersistentJob(dict):
    def __init__(self, job_id, *args, **kwargs):
        self._job_id = job_id
        super().__init__(*args, **kwargs)
    def __setitem__(self, key, value):
        with _lock:
            super().__setitem__(key, value)
            _save(self._job_id, self)
    def update(self, *args, **kwargs):
        with _lock:
            super().update(*args, **kwargs)
            _save(self._job_id, self)

class PersistentJobs(dict):
    def _hydrate(self, job_id):
        if dict.__contains__(self, job_id):
            return dict.__getitem__(self, job_id)
        data = _load(job_id)
        if data is None:
            return None
        job = PersistentJob(job_id, data)
        dict.__setitem__(self, job_id, job)
        return job
    def __contains__(self, job_id):
        return self._hydrate(job_id) is not None
    def __getitem__(self, job_id):
        job = self._hydrate(job_id)
        if job is None:
            raise KeyError(job_id)
        return job
    def __setitem__(self, job_id, value):
        job = PersistentJob(job_id, dict(value))
        dict.__setitem__(self, job_id, job)
        _save(job_id, job)
    def pop(self, job_id, *args):
        try:
            result = dict.pop(self, job_id)
        except KeyError:
            data = _load(job_id)
            if data is None:
                if args:
                    return args[0]
                raise
            result = PersistentJob(job_id, data)
        _delete(job_id)
        return result
