"""Integration tests for authorization, version conflicts and advisory reservations."""
import base64
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
        self.request('POST','/context/claims',self.a_token,{'path':'src/Foo','ttl_seconds':60},201)
        self.request('POST','/context/claims',self.b_token,{'path':'src/foo/file.py','ttl_seconds':60},409)
        self.request('DELETE','/context/claims?path=src/Foo',self.b_token,expected=404)
        self.request('DELETE','/context/claims?path=src/Foo',self.a_token)
        self.request('POST','/context/claims',self.b_token,{'path':'src/foo/file.py','ttl_seconds':60},201)

    def test_jsonl_append_is_append_only_versioned_and_checks_revision(self):
        item=self.request('POST','/context/items',self.a_token,{'title':'Auton handoff',
            'path':'context/auton-decisions.jsonl','content_type':'application/x-ndjson',
            'data_base64':content(b'{"type":"decision","text":"use SQLite initially"}')},201)['item']
        appended=self.request('POST',f"/context/items/{item['id']}/append",self.b_token,
            {'expected_revision':1,'record':{'type':'task','text':'Codex is checking profile isolation'}},201)
        self.assertEqual(appended['item']['revision'],2)
        current=self.request('GET',f"/context/items/{item['id']}/versions/2",self.a_token)
        self.assertEqual(base64.b64decode(current['data_base64']).splitlines(),[
            b'{"type":"decision","text":"use SQLite initially"}',
            b'{"type":"task","text":"Codex is checking profile isolation"}'])
        self.request('POST',f"/context/items/{item['id']}/append",self.a_token,
                     {'expected_revision':1,'record':{'type':'stale'}},409)

    def test_registration_and_anonymous_credentials_cannot_read_or_write(self):
        self.request('GET', '/context/items', None, expected=401)
        self.request('GET', '/context/items', self.join_key, expected=401)
        self.request('POST', '/context/items', self.a_token,
                     {'title':'Bad path','path':'../outside','data_base64':content(b'x')}, 422)

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

    def test_legacy_empty_workspace_not_visible_to_other_workspace(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute('UPDATE agents SET profile_id=NULL, workspace_id="" WHERE id=?', (self.a_id,))
            db.commit()
        friend = self.create_workspace(self.invite(), 'Isolated legacy test')
        profiles = friend['get']('/dashboard/profiles/data')['profiles']
        ids = {item['id'] for group in profiles for item in group.get('instances', [])}
        self.assertNotIn(self.a_id, ids)
