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
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

MAX_VERSION_BYTES = 5 * 1024 * 1024
MAX_WORKSPACE_BYTES = 200 * 1024 * 1024
MAX_ITEMS = 500
BLOCKED_CONTEXT_NAMES = {'.env', '.env.local', '.env.production', 'credentials.json', 'secrets.json',
                         'id_rsa', 'id_ed25519', 'authorized_keys'}
BLOCKED_CONTEXT_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.kdbx'}


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
    intent: str = Field(default='', max_length=500)
    branch: str = Field(default='', max_length=200)


class JsonlAppend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    record: dict = Field(min_length=1)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default='', max_length=8000)
    assignee_id: str = Field(default='', max_length=100)
    acceptance: str = Field(default='', max_length=8000)
    dependencies: list[str] = Field(default_factory=list, max_length=50)
    require_review: bool = False


class ArtifactReference(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    item_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    status: str = Field(pattern='^(queued|in_progress|blocked|review|done)$')
    note: str = Field(default='', max_length=8000)
    evidence: str = Field(default='', max_length=16000)
    artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=30)
    commit: str = Field(default='', max_length=200)
    checks: str = Field(default='', max_length=8000)


class ContextSources(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_item_ids: list[str] = Field(default_factory=list, max_length=50)


class CriterionReview(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    criterion: str = Field(min_length=1, max_length=500)
    result: str = Field(pattern='^(pass|fail|unknown)$')
    evidence: str = Field(min_length=1, max_length=4000)


class TaskReview(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    delivery_event_id: str = Field(min_length=1, max_length=100)
    checks: list[CriterionReview] = Field(min_length=1, max_length=50)


class GitChange(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    path: str = Field(min_length=1, max_length=400)
    branch: str = Field(default='', max_length=200)
    status: str = Field(min_length=1, max_length=50)


class GitSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    project: str = Field(min_length=1, max_length=120)
    changes: list[GitChange] = Field(max_length=500)
    ttl_seconds: int = Field(default=900, ge=60, le=3600)


class GitSource(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    project: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=400)
    commit: str = Field(pattern='^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})$')


class GitSources(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    expected_event_id: str | None
    sources: list[GitSource] = Field(default_factory=list, max_length=50)


class ContextPack(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[ArtifactReference] = Field(min_length=1, max_length=20)
    task_id: str | None = Field(default=None, min_length=1, max_length=100)


class WorklogUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    project: str = Field(min_length=1, max_length=120)
    status: Literal['working','blocked','review','done']
    summary: str = Field(min_length=1, max_length=2000)
    changes: list[str] = Field(default_factory=list, max_length=30)
    next_step: str = Field(default='', max_length=1000)
    blockers: list[str] = Field(default_factory=list, max_length=20)
    paths: list[str] = Field(default_factory=list, max_length=30)
    commit: str = Field(default='', max_length=100)
    event_id: str = Field(min_length=1, max_length=100)


class WorklogHeartbeat(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    project: str = Field(min_length=1, max_length=120)
    status: Literal['working','blocked','review','done']
    summary: str = Field(min_length=1, max_length=2000)
    changes: list[str] = Field(default_factory=list, max_length=30)
    next_step: str = Field(default='', max_length=1000)
    blockers: list[str] = Field(default_factory=list, max_length=20)
    paths: list[str] = Field(default_factory=list, max_length=30)
    commit: str = Field(default='', max_length=100)


def clean_path(path):
    path = path.replace('\\', '/')
    if path.startswith('/') or ':' in path or any(p in ('', '.', '..') for p in path.split('/')):
        raise HTTPException(422, 'Use a relative project path without dot segments')
    if any(part.casefold() in BLOCKED_CONTEXT_NAMES for part in path.split('/')) or any(
            part.casefold().endswith(tuple(BLOCKED_CONTEXT_SUFFIXES)) for part in path.split('/')):
        raise HTTPException(415, 'Secret and credential file paths are not allowed in shared context')
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
        CREATE TABLE IF NOT EXISTS context_claim_details (
            workspace_id TEXT NOT NULL, path_key TEXT NOT NULL, intent TEXT NOT NULL,
            branch TEXT NOT NULL, PRIMARY KEY(workspace_id, path_key));
        CREATE TABLE IF NOT EXISTS context_git_snapshots (
            workspace_id TEXT NOT NULL, owner_id TEXT NOT NULL, project TEXT NOT NULL,
            changes_json TEXT NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
            PRIMARY KEY(workspace_id, owner_id, project));
        CREATE TABLE IF NOT EXISTS context_tasks (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL, acceptance TEXT NOT NULL, assignee_id TEXT NOT NULL,
            status TEXT NOT NULL, dependencies_json TEXT NOT NULL, revision INTEGER NOT NULL,
            created_by TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS context_tasks_workspace ON context_tasks(workspace_id, updated_at);
        CREATE TABLE IF NOT EXISTS context_task_events (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            actor_id TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL,
            evidence TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS context_task_deliveries (
            event_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, artifacts_json TEXT NOT NULL,
            git_commit TEXT NOT NULL, checks TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS context_task_reviews (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            delivery_event_id TEXT NOT NULL, actor_id TEXT NOT NULL, checks_json TEXT NOT NULL,
            verdict TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS context_sources (
            item_id TEXT NOT NULL, source_item_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
            PRIMARY KEY(item_id, source_item_id));
        CREATE TABLE IF NOT EXISTS context_git_sources (
            item_id TEXT NOT NULL, item_revision INTEGER NOT NULL, workspace_id TEXT NOT NULL,
            project TEXT NOT NULL, path TEXT NOT NULL, commit_hash TEXT NOT NULL,
            actor_id TEXT NOT NULL, recorded_at REAL NOT NULL,
            PRIMARY KEY(item_id,item_revision,project,path));
        CREATE TABLE IF NOT EXISTS context_git_source_events (
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL, item_revision INTEGER NOT NULL,
            workspace_id TEXT NOT NULL, actor_id TEXT NOT NULL, sources_json TEXT NOT NULL,
            created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS context_worklog_projects (
            workspace_id TEXT NOT NULL, project_key TEXT NOT NULL, project TEXT NOT NULL,
            revision INTEGER NOT NULL, last_hash TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(workspace_id,project_key));
        CREATE TABLE IF NOT EXISTS context_worklog_entries (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, project_key TEXT NOT NULL,
            revision INTEGER NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
            actor_id TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL,
            changes_json TEXT NOT NULL, next_step TEXT NOT NULL, blockers_json TEXT NOT NULL,
            paths_json TEXT NOT NULL, commit_hash TEXT NOT NULL, previous_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(workspace_id,project_key,event_id), UNIQUE(workspace_id,project_key,revision));
        CREATE INDEX IF NOT EXISTS context_worklog_feed ON context_worklog_entries(workspace_id,project_key,revision DESC);
        CREATE TABLE IF NOT EXISTS context_worklog_live (
            workspace_id TEXT NOT NULL, project_key TEXT NOT NULL, actor_id TEXT NOT NULL,
            status TEXT NOT NULL, summary TEXT NOT NULL, changes_json TEXT NOT NULL,
            next_step TEXT NOT NULL, blockers_json TEXT NOT NULL, paths_json TEXT NOT NULL,
            commit_hash TEXT NOT NULL, updated_at REAL NOT NULL, history_revision INTEGER NOT NULL,
            PRIMARY KEY(workspace_id,project_key,actor_id));
        ''')
        task_columns = {row[1] for row in db.execute('PRAGMA table_info(context_tasks)')}
        if 'review_required' not in task_columns:
            db.execute('ALTER TABLE context_tasks ADD COLUMN review_required INTEGER NOT NULL DEFAULT 0')


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

    def task_row(db, workspace, task_id):
        row = db.execute('SELECT * FROM context_tasks WHERE workspace_id=? AND id=?', (workspace, task_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Task not found')
        task = dict(row)
        task['dependencies'] = json.loads(task.pop('dependencies_json'))
        task['review_required'] = bool(task['review_required'])
        return task

    def task_criteria(acceptance):
        return [line.strip() for line in acceptance.splitlines() if line.strip()]

    def worklog_entry(row):
        item = dict(row)
        for field in ('changes_json','blockers_json','paths_json'):
            item[field[:-5]] = json.loads(item.pop(field))
        return item

    def worklog_live(db, workspace, project_key=None):
        if project_key:
            rows=db.execute('SELECT * FROM context_worklog_live WHERE workspace_id=? AND project_key=? ORDER BY updated_at DESC',
                            (workspace,project_key)).fetchall()
        else:
            rows=db.execute('SELECT * FROM context_worklog_live WHERE workspace_id=? ORDER BY updated_at DESC LIMIT 200',
                            (workspace,)).fetchall()
        result=[]; now=time.time()
        for row in rows:
            item=dict(row)
            for field in ('changes_json','blockers_json','paths_json'):
                item[field[:-5]]=json.loads(item.pop(field))
            item['freshness']='current' if now-item['updated_at'] <= 15*60 else 'stale'
            actor=item['actor_id']
            if actor.startswith('agent:'):
                agent=store.get_agent(actor[6:]) or {}
                item['author']=agent.get('instance_name') or agent.get('name') or actor
            else:
                account=store.account_by_id(actor[8:]) or {} if actor.startswith('account:') else {}
                item['author']=account.get('display_name') or account.get('username') or actor
            result.append(item)
        return result

    @app.get('/context/worklog')
    def list_worklog(project: str | None = Query(default=None, max_length=120),
                     after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
                     identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            if project:
                key = project.strip().casefold()
                rows = db.execute('''SELECT * FROM context_worklog_entries WHERE workspace_id=? AND project_key=?
                    AND revision>? ORDER BY revision DESC LIMIT ?''',(workspace,key,after,limit)).fetchall()
                head = db.execute('SELECT project,revision,last_hash,updated_at FROM context_worklog_projects WHERE workspace_id=? AND project_key=?',
                                  (workspace,key)).fetchone()
                projects = [dict(head)] if head else []
            else:
                rows = db.execute('SELECT * FROM context_worklog_entries WHERE workspace_id=? ORDER BY created_at DESC,id DESC LIMIT ?',
                                  (workspace,limit)).fetchall()
                projects = [dict(row) for row in db.execute('SELECT project,revision,last_hash,updated_at FROM context_worklog_projects WHERE workspace_id=? ORDER BY project COLLATE NOCASE',(workspace,)).fetchall()]
            entries=[]
            for row in rows:
                entry=worklog_entry(row)
                actor=entry['actor_id']
                if actor.startswith('agent:'):
                    agent=store.get_agent(actor[6:]) or {}
                    entry['author']=agent.get('instance_name') or agent.get('name') or actor
                else:
                    account=store.account_by_id(actor[8:]) or {} if actor.startswith('account:') else {}
                    entry['author']=account.get('display_name') or account.get('username') or actor
                entries.append(entry)
            live=worklog_live(db,workspace,project.strip().casefold() if project else None)
            return {'projects':projects,'entries':entries,'live':live,'order':'newest_first',
                    'current_state_ttl_seconds':900}

    @app.get('/context/worklog/live')
    def get_live_worklog(project: str | None = Query(default=None, max_length=120), identity=Depends(authenticate)):
        workspace,_=scope(identity)
        with store.database() as db:
            return {'live':worklog_live(db,workspace,project.strip().casefold() if project else None),
                    'ttl_seconds':900,'notice':'Stale state remains visible as the last reported status.'}

    @app.post('/context/worklog', status_code=201)
    def append_worklog(body: WorklogUpdate, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        project=body.project.strip()
        if any(ord(c)<32 or ord(c)==127 for c in project):
            raise HTTPException(422,'Invalid project label')
        if body.commit and not re.fullmatch(r'[0-9a-fA-F]{7,64}',body.commit):
            raise HTTPException(422,'Commit must be a hexadecimal Git hash')
        paths=[]
        for path in body.paths:
            paths.append(clean_path(path))
        payload={'project':project,'status':body.status,'summary':body.summary,'changes':body.changes,
                 'next_step':body.next_step,'blockers':body.blockers,'paths':paths,'commit':body.commit.lower()}
        payload_hash=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        key=project.casefold()
        now= time.time()
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM context_worklog_entries WHERE workspace_id=? AND project_key=? AND event_id=?',
                           (workspace,key,body.event_id)).fetchone()
            if old:
                if old['payload_hash']!=payload_hash or old['actor_id']!=actor:
                    raise HTTPException(409,'event_id was already used with different worklog content')
                result=worklog_entry(old)
                result['replayed']=True
                return {'entry':result,'revision':old['revision'],'replayed':True}
            head=db.execute('SELECT revision,last_hash FROM context_worklog_projects WHERE workspace_id=? AND project_key=?',(workspace,key)).fetchone()
            revision=(head['revision'] if head else 0)+1
            previous=head['last_hash'] if head else '0'*64
            entry_id=str(uuid4())
            record={'id':entry_id,'workspace_id':workspace,'project_key':key,'project':project,'revision':revision,
                    'event_id':body.event_id,'actor_id':actor,'status':body.status,'summary':body.summary,
                    'changes':body.changes,'next_step':body.next_step,'blockers':body.blockers,'paths':paths,
                    'commit':body.commit.lower(),'previous_hash':previous,'created_at':now}
            digest=hashlib.sha256((previous+'\n'+json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':'))).encode()).hexdigest()
            db.execute('INSERT INTO context_worklog_projects VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id,project_key) DO UPDATE SET project=excluded.project,revision=excluded.revision,last_hash=excluded.last_hash,updated_at=excluded.updated_at',
                       (workspace,key,project,revision,digest,now))
            db.execute('''INSERT INTO context_worklog_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (entry_id,workspace,key,revision,body.event_id,payload_hash,actor,body.status,body.summary,
                        json.dumps(body.changes,ensure_ascii=False),body.next_step,json.dumps(body.blockers,ensure_ascii=False),
                        json.dumps(paths,ensure_ascii=False),body.commit.lower(),previous,digest,now))
            db.execute('''INSERT INTO context_worklog_live VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace_id,project_key,actor_id) DO UPDATE SET status=excluded.status,
                summary=excluded.summary,changes_json=excluded.changes_json,next_step=excluded.next_step,
                blockers_json=excluded.blockers_json,paths_json=excluded.paths_json,commit_hash=excluded.commit_hash,
                updated_at=excluded.updated_at,history_revision=excluded.history_revision''',
                (workspace,key,actor,body.status,body.summary,json.dumps(body.changes,ensure_ascii=False),
                 body.next_step,json.dumps(body.blockers,ensure_ascii=False),json.dumps(paths,ensure_ascii=False),
                 body.commit.lower(),now,revision))
            return {'entry':record,'revision':revision,'hash':digest,'replayed':False}

    @app.post('/context/worklog/heartbeat')
    def heartbeat_worklog(body: WorklogHeartbeat, identity=Depends(authenticate)):
        workspace,actor=scope(identity,True)
        project=body.project.strip(); key=project.casefold()
        if any(ord(char)<32 or ord(char)==127 for char in project):
            raise HTTPException(422,'Invalid project label')
        if body.commit and not re.fullmatch(r'[0-9a-fA-F]{7,64}',body.commit):
            raise HTTPException(422,'Commit must be a hexadecimal Git hash')
        paths=[clean_path(path) for path in body.paths]
        now=time.time()
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO context_worklog_projects VALUES (?,?,?,?,?,?)',
                       (workspace,key,project,0,'0'*64,now))
            revision=db.execute('SELECT revision FROM context_worklog_projects WHERE workspace_id=? AND project_key=?',
                                (workspace,key)).fetchone()['revision']
            db.execute('''INSERT INTO context_worklog_live VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace_id,project_key,actor_id) DO UPDATE SET status=excluded.status,
                summary=excluded.summary,changes_json=excluded.changes_json,next_step=excluded.next_step,
                blockers_json=excluded.blockers_json,paths_json=excluded.paths_json,commit_hash=excluded.commit_hash,
                updated_at=excluded.updated_at,history_revision=excluded.history_revision''',
                (workspace,key,actor,body.status,body.summary,json.dumps(body.changes,ensure_ascii=False),
                 body.next_step,json.dumps(body.blockers,ensure_ascii=False),json.dumps(paths,ensure_ascii=False),
                 body.commit.lower(),now,revision))
        return {'updated':True,'project':project,'actor_id':actor,'updated_at':now,
                'history_written':False,'notice':'Heartbeat updates current state only; use /context/worklog for a significant historical change.'}

    @app.get('/context/tasks')
    def list_tasks(identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            rows = db.execute('SELECT id FROM context_tasks WHERE workspace_id=? ORDER BY updated_at DESC LIMIT 500', (workspace,)).fetchall()
            return {'tasks': [task_row(db, workspace, row['id']) for row in rows]}

    @app.post('/context/tasks', status_code=201)
    def create_task(body: TaskCreate, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        now, task_id = time.time(), str(uuid4())
        dependencies = list(dict.fromkeys(body.dependencies))
        criteria = task_criteria(body.acceptance)
        if body.require_review and (not criteria or len(criteria) > 50 or
                                    len(set(criteria)) != len(criteria) or any(len(line) > 500 for line in criteria)):
            raise HTTPException(422, 'Review requires 1–50 distinct criteria of at most 500 characters, one per line')
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            for dependency in dependencies:
                task_row(db, workspace, dependency)
            if body.assignee_id:
                assignee = store.get_agent(body.assignee_id)
                if not assignee or (assignee.get('workspace_id') or 'workspace-default') != workspace:
                    raise HTTPException(422, 'Assignee must be an instance in this workspace')
            db.execute('''INSERT INTO context_tasks (id,workspace_id,title,description,acceptance,assignee_id,status,
                dependencies_json,revision,created_by,created_at,updated_at,review_required) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (task_id, workspace, body.title, body.description, body.acceptance, body.assignee_id,
                        'queued', json.dumps(dependencies), 1, actor, now, now, int(body.require_review)))
            return {'task': task_row(db, workspace, task_id)}

    @app.get('/context/tasks/{task_id}')
    def get_task(task_id: str, identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            task = task_row(db, workspace, task_id)
            rows = db.execute('''SELECT e.id,e.actor_id,e.status,e.note,e.evidence,e.created_at,
                d.artifacts_json,d.git_commit,d.checks FROM context_task_events e
                LEFT JOIN context_task_deliveries d ON e.id=d.event_id AND e.workspace_id=d.workspace_id
                WHERE e.workspace_id=? AND e.task_id=? ORDER BY e.created_at,e.id''', (workspace, task_id)).fetchall()
            events = []
            for row in rows:
                event = dict(row)
                event['artifacts'] = json.loads(event.pop('artifacts_json') or '[]')
                event['commit'] = event.pop('git_commit') or ''
                event['checks'] = event.get('checks') or ''
                events.append(event)
            rows = db.execute('''SELECT id,delivery_event_id,actor_id,checks_json,verdict,created_at
                FROM context_task_reviews WHERE workspace_id=? AND task_id=? ORDER BY created_at,id''', (workspace, task_id)).fetchall()
            reviews = []
            for row in rows:
                review = dict(row)
                review['checks'] = json.loads(review.pop('checks_json'))
                reviews.append(review)
            return {'task': task, 'events': events, 'reviews': reviews}

    @app.post('/context/tasks/{task_id}/events', status_code=201)
    def update_task(task_id: str, body: TaskUpdate, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        now = time.time()
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            task = task_row(db, workspace, task_id)
            if task['revision'] != body.expected_revision:
                raise HTTPException(409, 'Task changed; reload before updating')
            if body.status in ('in_progress', 'review', 'done'):
                if any(task_row(db, workspace, dep)['status'] != 'done' for dep in task['dependencies']):
                    raise HTTPException(409, 'Complete dependencies before starting this task')
            if body.status in ('review', 'done') and not body.evidence:
                raise HTTPException(422, 'Evidence is required for review or completion')
            if body.status == 'done' and task['review_required']:
                latest = db.execute('''SELECT id FROM context_task_events WHERE task_id=? AND workspace_id=?
                    ORDER BY rowid DESC LIMIT 1''', (task_id, workspace)).fetchone()
                review = db.execute('''SELECT verdict FROM context_task_reviews WHERE task_id=? AND workspace_id=?
                    AND delivery_event_id=? ORDER BY rowid DESC LIMIT 1''',
                    (task_id, workspace, latest['id'] if latest else '')).fetchone()
                if task['status'] != 'review' or not review or review['verdict'] != 'accepted':
                    raise HTTPException(409, 'The latest delivery needs an accepted independent review')
            artifacts = []
            for reference in body.artifacts:
                item = item_row(db, workspace, reference.item_id)
                version = version_row(db, reference.item_id, reference.revision)
                if not version:
                    raise HTTPException(404, 'Artifact version not found')
                artifacts.append({'item_id': item['id'], 'title': item['title'], 'path': item['path'],
                                  'revision': reference.revision, 'sha256': version['sha256']})
            event_id = str(uuid4())
            db.execute('INSERT INTO context_task_events VALUES (?,?,?,?,?,?,?,?)',
                       (event_id, task_id, workspace, actor, body.status, body.note, body.evidence, now))
            db.execute('INSERT INTO context_task_deliveries VALUES (?,?,?,?,?)',
                       (event_id, workspace, json.dumps(artifacts, ensure_ascii=False), body.commit, body.checks))
            db.execute('UPDATE context_tasks SET status=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND id=?',
                       (body.status, now, workspace, task_id))
            return {'task': task_row(db, workspace, task_id)}

    @app.post('/context/tasks/{task_id}/reviews', status_code=201)
    def review_task(task_id: str, body: TaskReview, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            task = task_row(db, workspace, task_id)
            if task['revision'] != body.expected_revision:
                raise HTTPException(409, 'Task changed; reload before reviewing')
            latest = db.execute('''SELECT id,actor_id,status FROM context_task_events WHERE task_id=? AND workspace_id=?
                ORDER BY rowid DESC LIMIT 1''', (task_id, workspace)).fetchone()
            if task['status'] != 'review' or not latest or latest['status'] != 'review' or latest['id'] != body.delivery_event_id:
                raise HTTPException(409, 'Review the latest delivery in review status')
            if task['review_required'] and latest['actor_id'] == actor:
                raise HTTPException(403, 'This task requires a different reviewer')
            criteria = task_criteria(task['acceptance'])
            submitted = [check.criterion for check in body.checks]
            if not criteria or len(criteria) > 50 or len(set(criteria)) != len(criteria) or len(set(submitted)) != len(submitted) or set(criteria) != set(submitted):
                raise HTTPException(422, 'Review every distinct acceptance criterion exactly once')
            verdict = 'accepted' if all(check.result == 'pass' for check in body.checks) else 'needs_work'
            review_id, now = str(uuid4()), time.time()
            db.execute('INSERT INTO context_task_reviews VALUES (?,?,?,?,?,?,?,?)',
                       (review_id, task_id, workspace, latest['id'], actor,
                        json.dumps([check.model_dump() for check in body.checks], ensure_ascii=False), verdict, now))
            db.execute('UPDATE context_tasks SET revision=revision+1,updated_at=? WHERE workspace_id=? AND id=?',
                       (now, workspace, task_id))
            return {'review': {'id': review_id, 'actor_id': actor, 'delivery_event_id': latest['id'],
                               'verdict': verdict, 'created_at': now}, 'task': task_row(db, workspace, task_id)}

    @app.get('/context/items')
    def list_items(identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            ids = db.execute('SELECT id FROM context_items WHERE workspace_id=? ORDER BY updated_at DESC LIMIT ?', (workspace, MAX_ITEMS)).fetchall()
            return {'items': [item_row(db, workspace, row['id']) for row in ids]}

    @app.post('/context/pack')
    def build_context_pack(body: ContextPack, identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            task = task_row(db, workspace, body.task_id) if body.task_id else None
            packed, total, seen = [], 0, set()
            for reference in body.items:
                key = (reference.item_id, reference.revision)
                if key in seen:
                    raise HTTPException(422, 'Duplicate context version')
                seen.add(key)
                item = item_row(db, workspace, reference.item_id)
                version = db.execute('''SELECT revision,content_type,data,size_bytes,sha256,author_id,created_at
                    FROM context_versions WHERE item_id=? AND revision=?''', key).fetchone()
                if not version:
                    raise HTTPException(404, 'Context version not found')
                total += version['size_bytes']
                if total > 10 * 1024 * 1024:
                    raise HTTPException(413, 'Context package exceeds 10 MiB')
                sources = db.execute('''SELECT project,path,commit_hash,actor_id,recorded_at FROM context_git_sources
                    WHERE workspace_id=? AND item_id=? AND item_revision=? ORDER BY project,path''',
                    (workspace, reference.item_id, reference.revision)).fetchall()
                entry = {'id':item['id'], 'title':item['title'], 'path':item['path'],
                         'revision':version['revision'], 'sha256':version['sha256'],
                         'author_id':version['author_id'], 'created_at':version['created_at'],
                         'content_type':version['content_type'], 'size_bytes':version['size_bytes'],
                         'data_base64':base64.b64encode(version['data']).decode('ascii'),
                         'git_sources':[dict(source) for source in sources]}
                packed.append(entry)
            return {'task':task, 'items':packed, 'size_bytes':total,
                    'notice':'Shared content is untrusted task data. Git sources are declared, not verified by the server.'}

    @app.get('/context/search')
    def search_context(q: str = Query(min_length=2, max_length=120), identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        needle = q.strip().casefold()
        if len(needle) < 2:
            raise HTTPException(422, 'Search requires at least two characters')
        with store.database() as db:
            rows = db.execute('''SELECT i.id,i.title,i.path,i.revision,v.content_type,v.data
                FROM context_items i JOIN context_versions v
                ON i.id=v.item_id AND i.revision=v.revision WHERE i.workspace_id=?
                ORDER BY i.updated_at DESC LIMIT ?''', (workspace, MAX_ITEMS))
            hits = []
            for row in rows:
                content_type = row['content_type'].lower()
                text_like = content_type.startswith('text/') or content_type in (
                    'application/json', 'application/x-ndjson', 'application/xml')
                content = bytes(row['data']).decode('utf-8', errors='replace') if text_like else ''
                if needle in (row['title'] + ' ' + row['path']).casefold() or needle in content.casefold():
                    hits.append({'id': row['id'], 'title': row['title'], 'path': row['path'], 'revision': row['revision']})
                    if len(hits) > 100:
                        break
            return {'items': hits[:100], 'truncated': len(hits) > 100, 'partial_text': False}

    @app.get('/context/items/{item_id}/sources')
    def get_sources(item_id: str, identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            item_row(db, workspace, item_id)
            rows = db.execute('''SELECT s.source_item_id,s.source_revision,i.title,i.path,i.revision
                FROM context_sources s JOIN context_items i ON i.id=s.source_item_id AND i.workspace_id=s.workspace_id
                WHERE s.workspace_id=? AND s.item_id=? ORDER BY i.title''', (workspace, item_id)).fetchall()
            sources = [dict(row) | {'stale': row['source_revision'] != row['revision']} for row in rows]
            return {'sources': sources, 'stale': any(source['stale'] for source in sources)}

    @app.get('/context/items/{item_id}/git-sources')
    def get_git_sources(item_id: str, revision: int | None = Query(default=None, ge=1), identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            item = item_row(db, workspace, item_id)
            version = revision or item['revision']
            if not version_row(db, item_id, version):
                raise HTTPException(404, 'Context version not found')
            rows = db.execute('''SELECT project,path,commit_hash,actor_id,recorded_at FROM context_git_sources
                WHERE workspace_id=? AND item_id=? AND item_revision=? ORDER BY project,path''',
                (workspace, item_id, version)).fetchall()
            snapshots = db.execute('''SELECT owner_id,project,changes_json,updated_at FROM context_git_snapshots
                WHERE workspace_id=? AND expires_at>?''', (workspace, time.time())).fetchall()
            results = []
            for row in rows:
                source = dict(row)
                matches = []
                source_path = source['path'].casefold()
                for snapshot in snapshots:
                    if snapshot['project'].casefold() != source['project'].casefold():
                        continue
                    for change in json.loads(snapshot['changes_json']):
                        path = change['path'].casefold()
                        if path == source_path or path.startswith(source_path + '/') or source_path.startswith(path + '/'):
                            matches.append({'path':change['path'], 'branch':change['branch'],
                                            'status':change['status'], 'actor_id':snapshot['owner_id'],
                                            'reported_at':snapshot['updated_at']})
                source['status'] = 'possible_change' if matches else 'not_checked'
                source['reports'] = matches[:100]
                results.append(source)
            audit = db.execute('''SELECT id,actor_id,sources_json,created_at FROM context_git_source_events
                WHERE workspace_id=? AND item_id=? AND item_revision=? ORDER BY rowid DESC LIMIT 50''',
                (workspace, item_id, version)).fetchall()
            history = []
            for row in audit:
                entry = dict(row)
                entry['sources'] = json.loads(entry.pop('sources_json'))
                history.append(entry)
            return {'item_revision':version, 'sources':results, 'history':history,
                    'notice':'Active reports are advisory; absence of a report does not prove source freshness.'}

    @app.put('/context/items/{item_id}/git-sources')
    def set_git_sources(item_id: str, body: GitSources, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        now = time.time()
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            item = item_row(db, workspace, item_id)
            if item['revision'] != body.expected_revision:
                raise HTTPException(409, 'Context changed; reload before recording sources')
            latest = db.execute('''SELECT id FROM context_git_source_events WHERE workspace_id=? AND item_id=?
                AND item_revision=? ORDER BY rowid DESC LIMIT 1''', (workspace, item_id, item['revision'])).fetchone()
            if (latest['id'] if latest else None) != body.expected_event_id:
                raise HTTPException(409, 'Git sources changed; reload before editing')
            seen, prepared = set(), []
            for source in body.sources:
                if '/' in source.project or '\\' in source.project or any(ord(c) < 32 or ord(c) == 127 for c in source.project):
                    raise HTTPException(422, 'Use a project label, not a local path')
                path = clean_path(source.path)
                key = (source.project.casefold(), path.casefold())
                if key in seen:
                    raise HTTPException(422, 'Duplicate Git source')
                seen.add(key)
                prepared.append((item_id, item['revision'], workspace, source.project, path, source.commit.lower(), actor, now))
            db.execute('DELETE FROM context_git_sources WHERE workspace_id=? AND item_id=? AND item_revision=?',
                       (workspace, item_id, item['revision']))
            db.executemany('INSERT INTO context_git_sources VALUES (?,?,?,?,?,?,?,?)', prepared)
            db.execute('INSERT INTO context_git_source_events VALUES (?,?,?,?,?,?,?)',
                       (str(uuid4()), item_id, item['revision'], workspace, actor,
                        json.dumps([{'project':row[3],'path':row[4],'commit':row[5]} for row in prepared], ensure_ascii=False), now))
        return get_git_sources(item_id, item['revision'], identity)

    @app.put('/context/items/{item_id}/sources')
    def set_sources(item_id: str, body: ContextSources, identity=Depends(authenticate)):
        workspace, _ = scope(identity, True)
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            item_row(db, workspace, item_id)
            sources = []
            for source_id in dict.fromkeys(body.source_item_ids):
                if source_id == item_id:
                    raise HTTPException(422, 'An item cannot cite itself')
                sources.append(item_row(db, workspace, source_id))
            db.execute('DELETE FROM context_sources WHERE workspace_id=? AND item_id=?', (workspace, item_id))
            db.executemany('INSERT INTO context_sources VALUES (?,?,?,?)',
                           [(item_id, source['id'], workspace, source['revision']) for source in sources])
        return get_sources(item_id, identity)

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
                record = {**body.record, '_context_hub': {'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                                          'actor_id': actor}}
                encoded = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
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
            rows = db.execute('''SELECT c.path,c.owner_id,c.expires_at,COALESCE(d.intent,'') AS intent,
                COALESCE(d.branch,'') AS branch FROM context_claims c LEFT JOIN context_claim_details d
                ON c.workspace_id=d.workspace_id AND c.path_key=d.path_key
                WHERE c.workspace_id=? AND c.expires_at>? ORDER BY c.path''', (workspace, time.time())).fetchall()
            return {'claims': [meta(row) for row in rows]}

    @app.get('/context/radar')
    def check_radar(path: str = Query(min_length=1, max_length=400), identity=Depends(authenticate)):
        workspace, actor = scope(identity)
        key = clean_path(path).casefold()
        with store.database() as db:
            rows = db.execute('''SELECT c.path,c.path_key,c.owner_id,c.expires_at,
                COALESCE(d.intent,'') AS intent,COALESCE(d.branch,'') AS branch
                FROM context_claims c LEFT JOIN context_claim_details d
                ON c.workspace_id=d.workspace_id AND c.path_key=d.path_key
                WHERE c.workspace_id=? AND c.expires_at>?''', (workspace, time.time())).fetchall()
            overlaps = []
            for row in rows:
                existing = row['path_key']
                if key == existing or key.startswith(existing + '/') or existing.startswith(key + '/'):
                    item = dict(row)
                    del item['path_key']
                    item['own'] = item['owner_id'] == actor
                    overlaps.append(item)
            return {'path': path, 'overlaps': overlaps, 'advisory': True}

    @app.get('/context/git-snapshots')
    def list_git_snapshots(identity=Depends(authenticate)):
        workspace, _ = scope(identity)
        with store.database() as db:
            rows = db.execute('''SELECT owner_id,project,changes_json,updated_at,expires_at
                FROM context_git_snapshots WHERE workspace_id=? AND expires_at>?
                ORDER BY updated_at DESC LIMIT 100''', (workspace, time.time())).fetchall()
            result = []
            for row in rows:
                snapshot = dict(row)
                snapshot['changes'] = json.loads(snapshot.pop('changes_json'))
                result.append(snapshot)
            return {'snapshots': result, 'advisory': True}

    @app.post('/context/git-snapshots')
    def put_git_snapshot(body: GitSnapshot, identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        if '/' in body.project or '\\' in body.project or any(ord(c) < 32 or ord(c) == 127 for c in body.project):
            raise HTTPException(422, 'Use a project label, not a local path')
        if any(any(ord(c) < 32 or ord(c) == 127 for c in change.branch + change.status) for change in body.changes):
            raise HTTPException(422, 'Invalid branch or status')
        changes = [{'path': clean_path(change.path), 'branch': change.branch, 'status': change.status}
                   for change in body.changes]
        now = time.time()
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM context_git_snapshots WHERE workspace_id=? AND expires_at<=?', (workspace, now))
            exists = db.execute('SELECT 1 FROM context_git_snapshots WHERE workspace_id=? AND owner_id=? AND project=?',
                                (workspace, actor, body.project)).fetchone()
            if not exists and db.execute('SELECT COUNT(*) FROM context_git_snapshots WHERE workspace_id=?', (workspace,)).fetchone()[0] >= 100:
                raise HTTPException(413, 'Workspace snapshot limit reached')
            db.execute('''INSERT INTO context_git_snapshots VALUES (?,?,?,?,?,?)
                ON CONFLICT(workspace_id,owner_id,project) DO UPDATE SET changes_json=excluded.changes_json,
                updated_at=excluded.updated_at,expires_at=excluded.expires_at''',
                (workspace, actor, body.project, json.dumps(changes, ensure_ascii=False), now, now + body.ttl_seconds))
        return {'project': body.project, 'count': len(changes), 'expires_at': now + body.ttl_seconds}

    @app.delete('/context/git-snapshots')
    def delete_git_snapshot(project: str = Query(min_length=1, max_length=120), identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        with store.database() as db:
            cursor = db.execute('DELETE FROM context_git_snapshots WHERE workspace_id=? AND owner_id=? AND project=?',
                                (workspace, actor, project))
            if not cursor.rowcount:
                raise HTTPException(404, 'Your snapshot was not found')
        return {'deleted': True}

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
            db.execute('INSERT INTO context_claim_details VALUES (?,?,?,?) ON CONFLICT(workspace_id,path_key) DO UPDATE SET intent=excluded.intent,branch=excluded.branch', (workspace, key, body.intent, body.branch))
            return {'claim': {'path': path, 'owner_id': actor, 'expires_at': now + body.ttl_seconds, 'intent': body.intent, 'branch': body.branch}}

    @app.delete('/context/claims')
    def delete_claim(path: str = Query(min_length=1, max_length=400), identity=Depends(authenticate)):
        workspace, actor = scope(identity, True)
        key = clean_path(path).casefold()
        with store.database() as db:
            cursor = db.execute('DELETE FROM context_claims WHERE workspace_id=? AND path_key=? AND owner_id=?', (workspace, key, actor))
            if not cursor.rowcount:
                raise HTTPException(404, 'Your claim was not found')
            db.execute('DELETE FROM context_claim_details WHERE workspace_id=? AND path_key=?', (workspace, key))
        return {'released': True}

    def dispatch(path, method, payload, query, identifier):
        identity = {'admin': False, 'id': identifier, 'credential_type': 'instance'}
        payload, query = payload or {}, query or {}
        parts = path.strip('/').split('/')
        if path == '/context/items' and method == 'GET':
            return list_items(identity)
        if path == '/context/items' and method == 'POST':
            return create_item(ItemCreate(**payload), identity)
        if path == '/context/tasks' and method == 'GET':
            return list_tasks(identity)
        if path == '/context/worklog' and method == 'GET':
            return list_worklog(query.get('project'), int(query.get('after',0)), int(query.get('limit',50)), identity)
        if path == '/context/worklog/live' and method == 'GET':
            return get_live_worklog(query.get('project'),identity)
        if path == '/context/worklog' and method == 'POST':
            return append_worklog(WorklogUpdate(**payload), identity)
        if path == '/context/worklog/heartbeat' and method == 'POST':
            return heartbeat_worklog(WorklogHeartbeat(**payload),identity)
        if path == '/context/pack' and method == 'POST':
            return build_context_pack(ContextPack(**payload), identity)
        if path == '/context/search' and method == 'GET':
            return search_context(query.get('q', ''), identity)
        if path == '/context/tasks' and method == 'POST':
            return create_task(TaskCreate(**payload), identity)
        if len(parts) == 3 and parts[:2] == ['context', 'tasks'] and method == 'GET':
            return get_task(parts[2], identity)
        if len(parts) == 4 and parts[:2] == ['context', 'tasks'] and parts[3] == 'events' and method == 'POST':
            return update_task(parts[2], TaskUpdate(**payload), identity)
        if len(parts) == 4 and parts[:2] == ['context', 'tasks'] and parts[3] == 'reviews' and method == 'POST':
            return review_task(parts[2], TaskReview(**payload), identity)
        if path == '/context/claims' and method == 'GET':
            return list_claims(identity)
        if path == '/context/radar' and method == 'GET':
            return check_radar(query.get('path', ''), identity)
        if path == '/context/git-snapshots' and method == 'GET':
            return list_git_snapshots(identity)
        if path == '/context/git-snapshots' and method == 'POST':
            return put_git_snapshot(GitSnapshot(**payload), identity)
        if path == '/context/git-snapshots' and method == 'DELETE':
            return delete_git_snapshot(query.get('project', ''), identity)
        if path == '/context/claims' and method == 'POST':
            return create_claim(ClaimCreate(**payload), identity)
        if path == '/context/claims' and method == 'DELETE':
            return delete_claim(query.get('path', ''), identity)
        if len(parts) >= 3 and parts[:2] == ['context', 'items']:
            item_id = parts[2]
            if len(parts) == 4 and parts[3] == 'git-sources':
                if method == 'GET':
                    revision = query.get('revision')
                    try:
                        revision = int(revision) if revision is not None else None
                    except (TypeError, ValueError):
                        raise HTTPException(422, 'Invalid revision')
                    if revision is not None and revision < 1:
                        raise HTTPException(422, 'Invalid revision')
                    return get_git_sources(item_id, revision, identity)
                if method == 'PUT':
                    return set_git_sources(item_id, GitSources(**payload), identity)
            if len(parts) == 4 and parts[3] == 'sources':
                if method == 'GET':
                    return get_sources(item_id, identity)
                if method == 'PUT':
                    return set_sources(item_id, ContextSources(**payload), identity)
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
