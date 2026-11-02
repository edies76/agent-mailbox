"""Integration tests for authorization, version conflicts and advisory reservations."""
import base64
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import test_mailbox as http_tests
import test_workspaces as workspace_tests


def content(value):
    return base64.b64encode(value).decode('ascii')


class ContextHubTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp
    invite = workspace_tests.WorkspaceTests.invite
    create_workspace = workspace_tests.WorkspaceTests.create_workspace

    def test_tasks_dependencies_evidence_and_workspace_isolation(self):
        first = self.request('POST', '/context/tasks', self.a_token,
                             {'title': 'Investigate', 'assignee_id': self.a_id}, 201)['task']
        second = self.request('POST', '/context/tasks', self.a_token,
                              {'title': 'Implement', 'dependencies': [first['id']]}, 201)['task']
        self.request('POST', f"/context/tasks/{second['id']}/events", self.b_token,
                     {'expected_revision': 1, 'status': 'in_progress'}, 409)
        self.request('POST', f"/context/tasks/{first['id']}/events", self.a_token,
                     {'expected_revision': 1, 'status': 'done'}, 422)
        artifact = self.request('POST', '/context/items', self.a_token,
                                {'title':'Evidence', 'path':'work/evidence.txt', 'data_base64':content(b'evidence')}, 201)['item']
        self.request('POST', f"/context/tasks/{first['id']}/events", self.a_token,
                     {'expected_revision': 1, 'status': 'done', 'evidence': 'Inspected APK resources',
                      'artifacts':[{'item_id':artifact['id'],'revision':1}], 'commit':'abc123', 'checks':'Tests passed'}, 201)
        self.request('POST', f"/context/tasks/{second['id']}/events", self.b_token,
                     {'expected_revision': 1, 'status': 'in_progress'}, 201)
        self.request('POST', f"/context/tasks/{second['id']}/events", self.b_token,
                     {'expected_revision': 1, 'status': 'blocked'}, 409)
        detail = self.request('GET', f"/context/tasks/{first['id']}", self.b_token)
        self.assertEqual(detail['events'][0]['evidence'], 'Inspected APK resources')
        self.assertEqual(detail['events'][0]['artifacts'][0]['sha256'], artifact['sha256'])
        self.assertEqual(detail['events'][0]['commit'], 'abc123')
        friend = self.create_workspace(self.invite(), 'Task isolation')
        friend_agent = friend['create_agent']('Task friend')
        self.request('GET', f"/context/tasks/{first['id']}", friend_agent['token'], expected=404)
        self.assertEqual(self.request('GET', '/context/tasks', friend_agent['token'])['tasks'], [])
        self.request('POST', '/context/tasks', friend_agent['token'],
                     {'title': 'Foreign dependency', 'dependencies': [first['id']]}, 404)

    def test_context_sources_report_stale_after_source_changes(self):
        source = self.request('POST', '/context/items', self.a_token,
                              {'title':'Source', 'path':'source.md', 'data_base64':content(b'v1')}, 201)['item']
        note = self.request('POST', '/context/items', self.a_token,
                            {'title':'Note', 'path':'note.md', 'data_base64':content(b'derived')}, 201)['item']
        url = f"/context/items/{note['id']}/sources"
        result = self.request('PUT', url, self.a_token, {'source_item_ids':[source['id']]})
        self.assertFalse(result['stale'])
        self.request('POST', f"/context/items/{source['id']}/versions", self.b_token,
                     {'expected_revision':1, 'data_base64':content(b'v2')}, 201)
        self.assertTrue(self.request('GET', url, self.a_token)['sources'][0]['stale'])
        self.request('PUT', url, self.a_token, {'source_item_ids':[note['id']]}, 422)

    def test_search_uses_current_text_and_observes_workspace_scope(self):
        item = self.request('POST', '/context/items', self.a_token,
                            {'title':'Find me', 'path':'search.txt', 'data_base64':content(b'rare keyword')}, 201)['item']
        hits = self.request('GET', '/context/search?q=keyword', self.b_token)['items']
        self.assertEqual([entry['id'] for entry in hits], [item['id']])
        self.request('POST', f"/context/items/{item['id']}/versions", self.a_token,
                     {'expected_revision':1, 'data_base64':content(b'different')}, 201)
        self.assertEqual(self.request('GET', '/context/search?q=keyword', self.a_token)['items'], [])
        friend = self.create_workspace(self.invite(), 'Search isolation')
        token = friend['create_agent']('Search friend')['token']
        self.assertEqual(self.request('GET', '/context/search?q=different', token)['items'], [])

    def test_search_finds_text_beyond_64k(self):
        value = b'a' * 70000 + b' distant-needle'
        item = self.request('POST', '/context/items', self.a_token,
                            {'title':'Long text', 'path':'long.txt', 'data_base64':content(value)}, 201)['item']
        result = self.request('GET', '/context/search?q=distant-needle', self.a_token)
        self.assertEqual([entry['id'] for entry in result['items']], [item['id']])
        self.assertFalse(result['partial_text'])

    def test_git_snapshots_are_scoped_expiring_and_owner_controlled(self):
        payload = {'project':'Auton', 'ttl_seconds':60,
                   'changes':[{'path':'app/input.kt', 'branch':'feature/input', 'status':' M'}]}
        self.request('POST', '/context/git-snapshots', self.a_token, payload)
        rows = self.request('GET', '/context/git-snapshots', self.b_token)['snapshots']
        self.assertEqual(rows[0]['owner_id'], 'agent:' + self.a_id)
        self.assertEqual(rows[0]['changes'][0]['path'], 'app/input.kt')
        self.request('DELETE', '/context/git-snapshots?project=Auton', self.b_token, expected=404)
        self.request('POST', '/context/git-snapshots', self.a_token,
                     {'project':'Auton', 'changes':[{'path':'.env', 'branch':'', 'status':'M'}]}, 415)
        friend = self.create_workspace(self.invite(), 'Git isolation')
        token = friend['create_agent']('Git friend')['token']
        self.assertEqual(self.request('GET', '/context/git-snapshots', token)['snapshots'], [])
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute('UPDATE context_git_snapshots SET expires_at=0 WHERE project=?', ('Auton',))
            db.commit()
        self.assertEqual(self.request('GET', '/context/git-snapshots', self.a_token)['snapshots'], [])
        self.request('DELETE', '/context/git-snapshots?project=Auton', self.a_token)
        self.assertEqual(self.request('GET', '/context/git-snapshots', self.a_token)['snapshots'], [])

    def test_workspace_isolation_versions_and_stale_write(self):
        created = self.request('POST', '/context/items', self.a_token,
                               {'title':'Plan', 'path':'docs/plan.md', 'data_base64':content(b'first')}, 201)
        item = created['item']
        self.assertEqual(item['revision'], 1)
        read = self.request('GET', '/context/items/' + item['id'] + '/versions/1', self.b_token)
        self.assertEqual(base64.b64decode(read['data_base64']), b'first')
        updated = self.request('POST', '/context/items/' + item['id'] + '/versions', self.b_token,
                               {'expected_revision':1, 'data_base64':content(b'second')}, 201)
        self.assertEqual(updated['item']['revision'], 2)
        self.request('POST', '/context/items/' + item['id'] + '/versions', self.a_token,
                     {'expected_revision':1, 'data_base64':content(b'overwrite')}, 409)
        self.assertEqual(len(self.request('GET', '/context/items/' + item['id'] + '/versions', self.a_token)['versions']), 2)
        friend = self.create_workspace(self.invite(), 'Private context')
        friend_agent = friend['create_agent']('Friend')
        self.request('GET', '/context/items/' + item['id'], friend_agent['token'], expected=404)
        self.assertEqual(friend['get']('/context/items')['items'], [])

    def test_claims_are_exclusive_including_parent_and_case(self):
        self.request('POST','/context/claims',self.a_token,{'path':'src/Foo','ttl_seconds':60,
                     'intent':'Change input tools','branch':'feature/input'},201)
        overlap = self.request('GET','/context/radar?path=src/foo/file.py',self.b_token)
        self.assertTrue(overlap['advisory'])
        self.assertEqual(overlap['overlaps'][0]['intent'],'Change input tools')
        self.assertEqual(overlap['overlaps'][0]['branch'],'feature/input')
        self.assertFalse(overlap['overlaps'][0]['own'])
        self.assertEqual(self.request('GET','/context/radar?path=other',self.b_token)['overlaps'], [])
        self.request('POST','/context/claims',self.b_token,{'path':'src/foo/file.py','ttl_seconds':60},409)
        self.request('DELETE','/context/claims?path=src/Foo',self.b_token,expected=404)
        self.request('DELETE','/context/claims?path=src/Foo',self.a_token)
        self.request('POST','/context/claims',self.b_token,{'path':'src/foo/file.py','ttl_seconds':60},201)

    def test_jsonl_append_is_append_only_versioned_and_checks_revision(self):
        item=self.request('POST','/context/items',self.a_token,{'title':'Auton handoff',
            'path':'context/auton-decisions.jsonl','content_type':'application/x-ndjson',
            'data_base64':content(b'{"type":"decision","text":"use SQLite initially"}')},201)['item']
        appended=self.request('POST',f"/context/items/{item['id']}/append",self.b_token,
            {'expected_revision':1,'record':{'type':'task','text':'Codex is checking profile isolation',
             '_context_hub':{'actor_id':'forged'}}},201)
        self.assertEqual(appended['item']['revision'],2)
        current=self.request('GET',f"/context/items/{item['id']}/versions/2",self.a_token)
        records=[json.loads(line) for line in base64.b64decode(current['data_base64']).splitlines()]
        self.assertEqual([{key:value for key,value in record.items() if key!='_context_hub'} for record in records],[
            {"type":"decision","text":"use SQLite initially"},
            {"type":"task","text":"Codex is checking profile isolation"}])
        self.assertEqual(records[1]['_context_hub']['actor_id'],'agent:'+self.b_id)
        self.assertNotEqual(records[1]['_context_hub']['actor_id'],'forged')
        self.request('POST',f"/context/items/{item['id']}/append",self.a_token,
                     {'expected_revision':1,'record':{'type':'stale'}},409)

    def test_registration_and_anonymous_credentials_cannot_read_or_write(self):
        self.request('GET', '/context/items', None, expected=401)
        self.request('GET', '/context/items', self.join_key, expected=401)
        self.request('POST', '/context/items', self.a_token,
                     {'title':'Bad path','path':'../outside','data_base64':content(b'x')}, 422)
        self.request('POST', '/context/items', self.a_token,
                     {'title':'Secret','path':'config/.env.production','data_base64':content(b'never share')}, 415)
        self.request('POST', '/context/items', self.a_token,
                     {'title':'Private key','path':'certs/server.pem','data_base64':content(b'never share')}, 415)

    def test_simultaneous_writes_do_not_overwrite(self):
        item = self.request('POST','/context/items',self.a_token,
                            {'title':'Concurrent','path':'conflict.txt','data_base64':content(b'v1')},201)['item']
        def write(token):
            import json
            from urllib.request import Request, urlopen
            from urllib.error import HTTPError
            req = Request(self.url + '/context/items/' + item['id'] + '/versions',
                          data=json.dumps({'expected_revision':1,'data_base64':content(b'next')}).encode(),
                          headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
            try:
                with urlopen(req,timeout=10) as response: return response.status
            except HTTPError as error:
                code = error.code
                error.close()
                return code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, [self.a_token, self.b_token]))
        self.assertEqual(sorted(results), [201,409])

    def test_remote_mcp_respects_context_workspace_isolation(self):
        import json
        from urllib.request import Request, urlopen
        def call(token, name, arguments):
            request = Request(self.url + '/mcp', data=json.dumps({'jsonrpc':'2.0','id':1,
                'method':'tools/call','params':{'name':name,'arguments':arguments}}).encode(), method='POST',
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json',
                         'Accept':'application/json, text/event-stream'})
            with urlopen(request, timeout=15) as response:
                return json.load(response)['result']
        result = call(self.a_token, 'mailbox_context_create',
                      {'title':'MCP','path':'mcp.txt','data_base64':content(b'secret')})
        self.assertFalse(result.get('isError', False), result)
        data = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        item_id = data['item']['id']
        result = call(self.b_token, 'mailbox_context_get', {'item_id':item_id,'revision':1})
        self.assertFalse(result.get('isError', False), result)
        data = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        self.assertEqual(base64.b64decode(data['data_base64']), b'secret')
        friend = self.create_workspace(self.invite(), 'MCP isolated')
        token = friend['create_agent']('MCP foreign')['token']
        denied = call(token, 'mailbox_context_get', {'item_id':item_id,'revision':1})
        self.assertTrue(denied.get('isError'), denied)
        task = call(self.a_token, 'mailbox_task_create', {'title':'MCP task'})
        self.assertFalse(task.get('isError', False), task)
        task_data = task.get('structuredContent') or json.loads(task['content'][0]['text'])
        task_id = task_data['task']['id']
        task = call(self.b_token, 'mailbox_task_update',
                    {'task_id':task_id, 'expected_revision':1, 'status':'done', 'evidence':'Checked'})
        self.assertFalse(task.get('isError', False), task)
        denied = call(token, 'mailbox_task_get', {'task_id':task_id})
        self.assertTrue(denied.get('isError'), denied)
        snapshot = call(self.a_token, 'mailbox_git_snapshot_publish',
                        {'project':'MCP','changes':[{'path':'src/main.py','branch':'feature','status':'M'}]})
        self.assertFalse(snapshot.get('isError', False), snapshot)
        listed = call(self.b_token, 'mailbox_git_snapshots', {})
        data = listed.get('structuredContent') or json.loads(listed['content'][0]['text'])
        self.assertEqual(data['snapshots'][0]['changes'][0]['path'], 'src/main.py')

    def test_legacy_empty_workspace_not_visible_to_other_workspace(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute('UPDATE agents SET profile_id=NULL, workspace_id="" WHERE id=?', (self.a_id,))
            db.commit()
        friend = self.create_workspace(self.invite(), 'Isolated legacy test')
        profiles = friend['get']('/dashboard/profiles/data')['profiles']
        ids = {item['id'] for group in profiles for item in group.get('instances', [])}
        self.assertNotIn(self.a_id, ids)
