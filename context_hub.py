"""Workspace-scoped shared artifacts and advisory path reservations.

Claims coordinate cooperating agents; they do not lock Git or a filesystem.
"""
import base64
import binascii
import hashlib
import json
import re
import time
from uuid import uuid4

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

MAX_VERSION_BYTES = 5 * 1024 * 1024
MAX_WORKSPACE_BYTES = 200 * 1024 * 1024
MAX_ITEMS = 500


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=400)
    content_type: str = Field(default="text/plain", min_length=1, max_length=120)
    data_base64: str = Field(max_length=7_100_000)


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    content_type: str = Field(default="text/plain", min_length=1, max_length=120)
    data_base64: str = Field(max_length=7_100_000)


class ClaimCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    path: str = Field(min_length=1, max_length=400)
    ttl_seconds: int = Field(default=900, ge=60, le=3600)


class JsonlAppend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    record: dict = Field(min_length=1)


def clean_path(path):
    path = path.replace('\\', '/')
    if path.startswith('/') or ':' in path or any(p in ('', '.', '..') for p in path.split('/')):
        raise HTTPException(422, 'Use a relative project path without dot segments')
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise HTTPException(422, 'Invalid project path')
    return path


def decode_content(value, content_type):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]*/[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]*', content_type):
        raise HTTPException(422, 'Invalid content type')
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, 'Invalid base64')
    if len(data) > MAX_VERSION_BYTES:
        raise HTTPException(413, 'Maximum version size is 5 MiB')
    return data


def initialize_context(store):
    if not hasattr(store, 'database'):
        return
    with store.database() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS context_items (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT NOT NULL,
            path TEXT NOT NULL, path_key TEXT NOT NULL, revision INTEGER NOT NULL,
            author_id TEXT NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(workspace_id, path_key));
        CREATE TABLE IF NOT EXISTS context_versions (
            item_id TEXT NOT NULL REFERENCES context_items(id), revision INTEGER NOT NULL,
            content_type TEXT NOT NULL, data BLOB NOT NULL, size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL, author_id TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(item_id, revision));
        CREATE TABLE IF NOT EXISTS context_claims (
            workspace_id TEXT NOT NULL, path TEXT NOT NULL, path_key TEXT NOT NULL,
            owner_id TEXT NOT NULL, expires_at REAL NOT NULL,
            PRIMARY KEY(workspace_id, path_key));
        ''')


def register_context_routes(app, store, authenticate, identity_workspace):
    def scope(identity, write=False):
        if not hasattr(store, 'database'):
            raise HTTPException(501, 'Context Hub is currently available only on SQLite')
        if identity.get('registration_key'):
            raise HTTPException(403, 'Registration keys cannot access shared context')
        if identity.get('credential_type') in ('instance', 'api_key'):
            agent = store.get_agent(identity['id'])
            if not agent:
                raise HTTPException(401, 'Instance unavailable')
            workspace = agent.get('workspace_id') or 'workspace-default'
            return workspace, 'agent:' + identity['id']
        if identity.get('account_session_id') and identity.get('workspace_scoped'):
            if write and identity.get('role') not in ('owner', 'workspace_owner', 'workspace_member', 'operator'):
                raise HTTPException(403, 'Account cannot modify shared context')
            return identity_workspace(identity), 'account:' + identity['id']
        raise HTTPException(403, 'An instance token or account session is required')

    def meta(row):
        return dict(row)

    def validate_quota(db, workspace, added):
        used = db.execute('SELECT COALESCE(SUM(v.size_bytes),0) FROM context_versions v JOIN context_items i ON i.id=v.item_id WHERE i.workspace_id=?', (workspace,)).fetchone()[0]
        if used + added > MAX_WORKSPACE_BYTES:
            raise HTTPException(413, 'Workspace context quota exceeded')

    def version_row(db, item_id, revision):
        return db.execute('SELECT revision,content_type,size_bytes,sha256,author_id,created_at FROM context_versions WHERE item_id=? AND revision=?', (item_id, revision)).fetchone()

    def item_row(db, workspace, item_id):
        row = db.execute('SELECT id,title,path,revision,author_id,updated_at FROM context_items WHERE workspace_id=? AND id=?', (workspace, item_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Context item not found')
        current = version_row(db, item_id, row['revision'])
        return meta(row) | {'content_type': current['content_type'], 'size_bytes': current['size_bytes'], 'sha256': current['sha256']}

    @app.get('/context/items')
    def list_items(identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            ids = db.execute('SELECT id FROM context_items WHERE workspace_id=? ORDER BY updated_at DESC LIMIT ?', (workspace, MAX_ITEMS)).fetchall()
            return {'items': [item_row(db, workspace, row['id']) for row in ids]}

    @app.post('/context/items', status_code=201)
    def create_item(body: ItemCreate, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        path = clean_path(body.path)
        data = decode_content(body.data_base64, body.content_type)
        now, item_id = time.time(), str(uuid4())
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM context_items WHERE workspace_id=? AND path_key=?', (workspace, path.casefold())).fetchone():
                raise HTTPException(409, 'Path already exists in this workspace')
            if db.execute('SELECT COUNT(*) FROM context_items WHERE workspace_id=?', (workspace,)).fetchone()[0] >= MAX_ITEMS:
                raise HTTPException(413, 'Workspace context item limit reached')
            validate_quota(db, workspace, len(data))
            db.execute('INSERT INTO context_items VALUES (?,?,?,?,?,?,?,?)', (item_id, workspace, body.title, path, path.casefold(), 1, actor, now))
            db.execute('INSERT INTO context_versions VALUES (?,?,?,?,?,?,?,?)', (item_id, 1, body.content_type, data, len(data), hashlib.sha256(data).hexdigest(), actor, now))
            return {'item': item_row(db, workspace, item_id)}

    @app.get('/context/items/{item_id}')
    def get_item(item_id: str, identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            return {'item': item_row(db, workspace, item_id)}

    @app.get('/context/items/{item_id}/versions')
    def list_versions(item_id: str, identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            item_row(db, workspace, item_id)
            rows = db.execute('SELECT revision,content_type,size_bytes,sha256,author_id,created_at FROM context_versions WHERE item_id=? ORDER BY revision DESC LIMIT 200', (item_id,)).fetchall()
            return {'versions': [meta(row) for row in rows]}

    @app.get('/context/items/{item_id}/versions/{revision}')
    def get_version(item_id: str, revision: int, identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            item_row(db, workspace, item_id)
            row = db.execute('SELECT revision,content_type,data,size_bytes,sha256,author_id,created_at FROM context_versions WHERE item_id=? AND revision=?', (item_id, revision)).fetchone()
            if not row:
                raise HTTPException(404, 'Version not found')
            version = meta(row)
            data = version.pop('data')
            return {'version': version, 'data_base64': base64.b64encode(data).decode('ascii')}

    @app.post('/context/items/{item_id}/versions', status_code=201)
    def create_version(item_id: str, body: VersionCreate, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        data = decode_content(body.data_base64, body.content_type)
        now = time.time()
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            item = item_row(db, workspace, item_id)
            if item['revision'] != body.expected_revision:
                raise HTTPException(409, 'Version changed; reload before writing')
            validate_quota(db, workspace, len(data))
            revision = item['revision'] + 1
            db.execute('INSERT INTO context_versions VALUES (?,?,?,?,?,?,?,?)', (item_id, revision, body.content_type, data, len(data), hashlib.sha256(data).hexdigest(), actor, now))
            db.execute('UPDATE context_items SET revision=?,author_id=?,updated_at=? WHERE id=? AND workspace_id=?', (revision, actor, now, item_id, workspace))
            return {'item': item_row(db, workspace, item_id)}

    @app.post('/context/items/{item_id}/append', status_code=201)
    def append_jsonl(item_id: str, body: JsonlAppend, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute('SELECT id,path,revision FROM context_items WHERE workspace_id=? AND id=?',
                              (workspace, item_id)).fetchone()
            if not item:
                raise HTTPException(404, 'Context item not found')
            if not item['path'].casefold().endswith(('.jsonl', '.ndjson')):
                raise HTTPException(415, 'Append is available only for .jsonl or .ndjson resources')
            if item['revision'] != body.expected_revision:
                raise HTTPException(409, 'Version changed; reload before appending')
            try:
                encoded = json.dumps(body.record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            except (TypeError, ValueError):
                raise HTTPException(422, 'Record must be JSON serializable')
            if len(encoded) > 32 * 1024:
                raise HTTPException(413, 'Each JSONL record is limited to 32 KiB')
            previous = db.execute('SELECT data FROM context_versions WHERE item_id=? AND revision=?',
                                  (item_id, item['revision'])).fetchone()
            prior_data = bytes(previous['data'])
            if prior_data:
                try:
                    lines = prior_data.decode('utf-8').splitlines()
                    if any(not line.strip() or not isinstance(json.loads(line), dict) for line in lines):
                        raise ValueError('Existing JSONL must contain one JSON object per line')
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    raise HTTPException(422, 'Existing resource is not valid JSONL') from error
            data = prior_data + (b'\n' if prior_data and not prior_data.endswith(b'\n') else b'') + encoded + b'\n'
            if len(data) > MAX_VERSION_BYTES:
                raise HTTPException(413, 'Maximum version size is 5 MiB')
            validate_quota(db, workspace, len(data))
            revision = item['revision'] + 1
            now = time.time()
            db.execute('INSERT INTO context_versions VALUES (?,?,?,?,?,?,?,?)',
                       (item_id, revision, 'application/x-ndjson', data, len(data),
                        hashlib.sha256(data).hexdigest(), actor, now))
            db.execute('UPDATE context_items SET revision=?,author_id=?,updated_at=? WHERE id=? AND workspace_id=?',
                       (revision, actor, now, item_id, workspace))
            return {'item': item_row(db, workspace, item_id), 'appended_revision': revision}

    @app.get('/context/claims')
    def list_claims(identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            rows = db.execute('SELECT path,owner_id,expires_at FROM context_claims WHERE workspace_id=? AND expires_at>? ORDER BY path', (workspace, time.time())).fetchall()
            return {'claims': [meta(row) for row in rows]}

    @app.post('/context/claims', status_code=201)
    def create_claim(body: ClaimCreate, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        path = clean_path(body.path)
        key = path.casefold()
        now = time.time()
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT path,path_key,owner_id,expires_at FROM context_claims WHERE workspace_id=? AND expires_at>?', (workspace, now)).fetchall()
            for row in rows:
                if (key == row['path_key'] or key.startswith(row['path_key'] + '/') or row['path_key'].startswith(key + '/')) and row['owner_id'] != actor:
                    raise HTTPException(409, 'Path is already claimed by another participant')
            db.execute('INSERT INTO context_claims VALUES (?,?,?,?,?) ON CONFLICT(workspace_id,path_key) DO UPDATE SET path=excluded.path,owner_id=excluded.owner_id,expires_at=excluded.expires_at', (workspace, path, key, actor, now + body.ttl_seconds))
            return {'claim': {'path': path, 'owner_id': actor, 'expires_at': now + body.ttl_seconds}}

    @app.delete('/context/claims')
    def delete_claim(path: str = Query(min_length=1, max_length=400), identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        key = clean_path(path).casefold()
        with store.database() as db:
            cursor = db.execute('DELETE FROM context_claims WHERE workspace_id=? AND path_key=? AND owner_id=?', (workspace, key, actor))
            if not cursor.rowcount:
                raise HTTPException(404, 'Your claim was not found')
        return {'released': True}

    def dispatch(path, method, payload, query, identifier):
        identity = {'admin': False, 'id': identifier, 'credential_type': 'instance'}
        payload, query = payload or {}, query or {}
        parts = path.strip('/').split('/')
        if path == '/context/items' and method == 'GET':
            return list_items(identity)
        if path == '/context/items' and method == 'POST':
            return create_item(ItemCreate(**payload), identity)
        if path == '/context/claims' and method == 'GET':
            return list_claims(identity)
        if path == '/context/claims' and method == 'POST':
            return create_claim(ClaimCreate(**payload), identity)
        if path == '/context/claims' and method == 'DELETE':
            return delete_claim(query.get('path', ''), identity)
        if len(parts) >= 3 and parts[:2] == ['context', 'items']:
            item_id = parts[2]
            if len(parts) == 3 and method == 'GET':
                return get_item(item_id, identity)
            if len(parts) == 4 and parts[3] == 'versions':
                if method == 'GET':
                    return list_versions(item_id, identity)
                if method == 'POST':
                    return create_version(item_id, VersionCreate(**payload), identity)
            if len(parts) == 4 and parts[3] == 'append' and method == 'POST':
                return append_jsonl(item_id, JsonlAppend(**payload), identity)
            if len(parts) == 5 and parts[3] == 'versions' and method == 'GET':
                try:
                    revision = int(parts[4])
                except ValueError:
                    raise HTTPException(422, 'Invalid version')
                return get_version(item_id, revision, identity)
        raise HTTPException(404, 'Unknown context route')

    return dispatch
