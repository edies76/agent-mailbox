"""Persistent mailbox storage: local SQLite or a dedicated Firestore database."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import json
import secrets

from fastapi import HTTPException
from collaboration import (CollaborationStore, DEFAULT_WORKSPACE_ID, workspace_connection_id,
                           message_view, message_fields)
from delivery import delivery_identity


def check_reply(original, sender, recipient):
    if original is None or original["to_agent_id"] != sender or original["from_agent_id"] != recipient:
        raise HTTPException(404, "Original message not found in this conversation")


class SQLiteStore(CollaborationStore):
    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.database() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    project TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL, last_seen REAL NOT NULL,
                    description TEXT NOT NULL DEFAULT '', capabilities_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_agent_id TEXT NOT NULL REFERENCES agents(id),
                    to_agent_id TEXT NOT NULL REFERENCES agents(id),
                    text TEXT NOT NULL, in_reply_to INTEGER REFERENCES messages(id),
                    created_at REAL NOT NULL, acknowledged_at REAL,
                    attachments_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS inbox_idx ON messages(to_agent_id, id);
                CREATE INDEX IF NOT EXISTS unread_idx
                    ON messages(to_agent_id, id) WHERE acknowledged_at IS NULL;
                CREATE INDEX IF NOT EXISTS online_idx ON agents(last_seen);
                CREATE TABLE IF NOT EXISTS agent_api_keys (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, key_prefix TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL, last_used_at REAL, revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS agent_api_key_hash_idx ON agent_api_keys(token_hash);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if "attachments_json" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
            agent_columns = {row[1] for row in db.execute("PRAGMA table_info(agents)")}
            for column, definition in (("listener_seen_at", "REAL"), ("listener_ttl", "INTEGER"),
                                       ("wake_configured", "INTEGER")):
                if column not in agent_columns:
                    db.execute(f"ALTER TABLE agents ADD COLUMN {column} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS attachments (
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                attachment_index INTEGER NOT NULL, filename TEXT NOT NULL, media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, data BLOB NOT NULL,
                PRIMARY KEY(message_id, attachment_index)
            )""")
        self.initialize_collaboration()

    @staticmethod
    def message_output(row):
        result = dict(row)
        result["attachments"] = json.loads(result.pop("attachments_json", "[]"))
        return message_view(result)

    def ping(self):
        with self.database() as db:
            db.execute("SELECT 1").fetchone()

    def authenticate(self, digest):
        with self.database() as db:
            row = db.execute("SELECT id FROM agents WHERE token_hash = ?", (digest,)).fetchone()
            if row:
                db.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (time.time(), row["id"]))
                return row["id"]
        return None

    def authenticate_agent_api_key(self, digest):
        with self.database() as db:
            row = db.execute("SELECT k.id,k.agent_id FROM agent_api_keys k JOIN agents a ON a.id=k.agent_id "
                             "WHERE k.token_hash = ? AND k.revoked_at IS NULL AND k.credential_epoch=a.credential_epoch",
                             (digest,)).fetchone()
            if not row:
                return None
            seen = time.time()
            db.execute("UPDATE agent_api_keys SET last_used_at = ? WHERE id = ?", (seen, row["id"]))
            db.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (seen, row["agent_id"]))
            return {"id": row["agent_id"], "api_key_id": row["id"]}

    def create_agent_api_key(self, row, expected_instance_hash):
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute("SELECT token_hash,credential_epoch FROM agents WHERE id=?", (row["agent_id"],)).fetchone()
            if not agent or not secrets.compare_digest(agent["token_hash"], expected_instance_hash):
                raise HTTPException(401, "Instance credential changed; sign in again")
            db.execute("""INSERT INTO agent_api_keys
                (id,agent_id,name,key_prefix,token_hash,created_at,last_used_at,revoked_at,credential_epoch)
                VALUES (?,?,?,?,?,?,NULL,NULL,?)""",
                (row["id"], row["agent_id"], row["name"], row["key_prefix"], row["token_hash"], row["created_at"], agent["credential_epoch"]))
        return row

    def agent_api_keys(self, agent_id):
        with self.database() as db:
            rows = db.execute("SELECT k.id,k.agent_id,k.name,k.key_prefix,k.created_at,k.last_used_at,k.revoked_at,"
                              "k.credential_epoch,a.credential_epoch AS current_epoch,a.credential_rotated_at "
                              "FROM agent_api_keys k JOIN agents a ON a.id=k.agent_id WHERE k.agent_id=? ORDER BY k.created_at DESC",
                              (agent_id,)).fetchall()
            return [dict(row) | {"revoked_at": row["revoked_at"] or (row["credential_rotated_at"] if row["credential_epoch"] != row["current_epoch"] else None)}
                    for row in rows]

    def rotate_agent_token(self, agent_id, token_hash, recovery_hash, expected_epoch, now):
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute("SELECT token_hash,recovery_hash,last_rotation_hash,credential_epoch FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                raise HTTPException(404, "Agent not found")
            if agent["token_hash"] == token_hash and agent["recovery_hash"] == recovery_hash:
                if agent["last_rotation_hash"] == recovery_hash and agent["credential_epoch"] == expected_epoch + 1:
                    return False
                raise HTTPException(409, "Choose a new recovery secret for token rotation")
            if agent["credential_epoch"] != expected_epoch:
                raise HTTPException(409, "Instance credential changed; refresh before rotating")
            db.execute("UPDATE agents SET token_hash=?,credential_epoch=credential_epoch+1,credential_rotated_at=?,recovery_hash=?,last_rotation_hash=? WHERE id=?",
                       (token_hash, now, recovery_hash, recovery_hash, agent_id))
            db.execute("UPDATE agent_api_keys SET revoked_at=? WHERE agent_id=? AND revoked_at IS NULL", (now, agent_id))
            return True

    def revoke_agent_api_key(self, agent_id, key_id):
        with self.database() as db:
            cursor = db.execute("UPDATE agent_api_keys SET revoked_at = ? WHERE agent_id = ? AND id = ? AND revoked_at IS NULL",
                                (time.time(), agent_id, key_id))
            return bool(cursor.rowcount)

    def register(self, row):
        with self.database() as db:
            try:
                db.execute("INSERT INTO agents (id, name, kind, project, token_hash, created_at, last_seen, description, capabilities_json, chat_path, workspace_id, recovery_hash, created_by_account_id, created_by_username, registration_method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           tuple(row.get(k, "") for k in ("id", "name", "kind", "project", "token_hash", "created_at", "last_seen", "description")) +
                           (json.dumps(row.get("capabilities", []), ensure_ascii=False), row.get("chat_path", ""),
                            row.get("workspace_id", DEFAULT_WORKSPACE_ID), row.get("recovery_hash"),
                            row.get("created_by_account_id"), row.get("created_by_username", ""), row.get("registration_method", "unknown")))
            except sqlite3.IntegrityError:
                if db.execute("SELECT 1 FROM agents WHERE id=?", (row["id"],)).fetchone():
                    return False
                raise
        return True

    @staticmethod
    def agent_output(row):
        if not row:
            return None
        result = dict(row)
        result["capabilities"] = json.loads(result.pop("capabilities_json", "[]"))
        return result

    def all_agents(self):
        with self.database() as db:
            return [self.agent_output(row) for row in db.execute("SELECT * FROM agents ORDER BY created_at, id")]

    def get_agent(self, identifier):
        with self.database() as db:
            row = db.execute("SELECT * FROM agents WHERE id = ?", (identifier,)).fetchone()
        return self.agent_output(row)

    def update_agent_card(self, identifier, fields):
        with self.database() as db:
            if "name" in fields:
                db.execute("UPDATE agents SET name = ? WHERE id = ?", (fields["name"], identifier))
                db.execute("UPDATE agents SET instance_name = ? WHERE id = ? AND profile_id IS NOT NULL",
                           (fields["name"], identifier))
            if "description" in fields:
                db.execute("UPDATE agents SET description = ? WHERE id = ?", (fields["description"], identifier))
            if "capabilities" in fields:
                db.execute("UPDATE agents SET capabilities_json = ? WHERE id = ?",
                           (json.dumps(fields["capabilities"], ensure_ascii=False), identifier))
            if "chat_path" in fields:
                db.execute("UPDATE agents SET chat_path = ? WHERE id = ?", (fields["chat_path"], identifier))
            row = db.execute("SELECT * FROM agents WHERE id = ?", (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, "Agent not found")
        return self.agent_output(row)

    def report_listener(self, identifier, seen_at, ttl, wake_configured):
        with self.database() as db:
            db.execute("UPDATE agents SET listener_seen_at=?, listener_ttl=?, wake_configured=? WHERE id=?",
                       (seen_at, ttl, int(wake_configured), identifier))

    def unregister(self, identifier):
        """Revoke an agent and permanently erase its messages and attachments."""
        with self.database() as db:
            if db.execute("SELECT 1 FROM agents WHERE id = ?", (identifier,)).fetchone() is None:
                return False
            db.execute("DELETE FROM messages WHERE from_agent_id = ? OR to_agent_id = ?",
                       (identifier, identifier))
            db.execute("DELETE FROM agents WHERE id = ?", (identifier,))
            db.execute("DELETE FROM delivery_keys WHERE sender_id = ?", (identifier,))
        return True

    def stats(self, cutoff):
        with self.database() as db:
            agents = db.execute("SELECT COUNT(*), COALESCE(SUM(last_seen >= ?), 0) FROM agents", (cutoff,)).fetchone()
            messages = db.execute("SELECT COUNT(*), COALESCE(SUM(acknowledged_at IS NULL), 0) FROM messages").fetchone()
        return dict(agents_total=agents[0], agents_online=agents[1], messages_total=messages[0], messages_pending=messages[1])

    def send(self, sender, body, attachments=None, finalize=None):
        attachments = attachments or []
        key_hash, payload_hash = delivery_identity(sender, body, attachments, finalize)
        metadata = [{key: item[key] for key in ("filename", "media_type", "size_bytes")} | {"index": i}
                    for i, item in enumerate(attachments)]
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            if key_hash:
                cached = db.execute("SELECT * FROM delivery_keys WHERE key_hash = ?", (key_hash,)).fetchone()
                if cached:
                    if cached["payload_hash"] != payload_hash:
                        raise HTTPException(409, "Idempotency key was used for different content")
                    previous = db.execute("SELECT * FROM messages WHERE id = ?", (cached["message_id"],)).fetchone()
                    if previous is None:
                        raise HTTPException(410, "This delivery was deleted; do not retry it with this key")
                    return {**self.message_output(previous), "delivery_replayed": True}
            if db.execute("SELECT 1 FROM agents WHERE id = ?", (body.to_agent_id,)).fetchone() is None:
                raise HTTPException(404, "Recipient not found")
            sender_row = db.execute("SELECT workspace_id FROM agents WHERE id = ?", (sender,)).fetchone()
            recipient_row = db.execute("SELECT workspace_id FROM agents WHERE id = ?", (body.to_agent_id,)).fetchone()
            if not sender_row:
                raise HTTPException(401, "Sender instance is unavailable")
            sender_workspace = sender_row["workspace_id"] or DEFAULT_WORKSPACE_ID
            recipient_workspace = recipient_row["workspace_id"] or DEFAULT_WORKSPACE_ID
            if sender_workspace != recipient_workspace:
                connection_id = workspace_connection_id(sender_workspace, recipient_workspace)
                connection = db.execute("SELECT status FROM workspace_connections WHERE id = ?", (connection_id,)).fetchone()
                if not connection or connection["status"] != "accepted":
                    raise HTTPException(403, "These workspaces are not connected")
            original = None
            if body.in_reply_to is not None:
                original = db.execute("SELECT * FROM messages WHERE id = ?", (body.in_reply_to,)).fetchone()
                check_reply(original, sender, body.to_agent_id)
                original = self.message_output(original)
                if finalize and original["status"] in ("completed", "cancelled"):
                    raise HTTPException(409, "Request is already closed")
            fields = message_fields(body, None, original)
            cursor = db.execute("""INSERT INTO messages
                (from_agent_id, to_agent_id, text, in_reply_to, created_at, attachments_json,
                 subject, thread_id, status, status_note, updated_at, share_subject, message_type,
                 delivery_mode, wake_backlog)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sender, body.to_agent_id, body.text, body.in_reply_to, time.time(),
                 json.dumps(metadata, ensure_ascii=False), fields["subject"], fields["thread_id"],
                 fields["status"], fields["status_note"], fields["updated_at"], int(fields["share_subject"]),
                 fields["message_type"], fields["delivery_mode"], int(fields["wake_backlog"])))
            message_id = cursor.lastrowid
            if key_hash:
                db.execute("INSERT INTO delivery_keys VALUES (?, ?, ?, ?, ?)",
                           (key_hash, sender, payload_hash, message_id, time.time()))
            if fields["thread_id"] is None:
                db.execute("UPDATE messages SET thread_id = ? WHERE id = ?", (message_id, message_id))
            if finalize:
                db.execute("UPDATE messages SET status = ?, status_note = '', updated_at = ?, acknowledged_at = "
                           "CASE WHEN ? = 'completed' THEN COALESCE(acknowledged_at, ?) ELSE acknowledged_at END "
                           "WHERE id = ?", (finalize["status"], time.time(), finalize["status"], time.time(), body.in_reply_to))
            for index, item in enumerate(attachments):
                db.execute("""INSERT INTO attachments
                    (message_id, attachment_index, filename, media_type, size_bytes, data)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (message_id, index, item["filename"], item["media_type"], item["size_bytes"], item["data"]))
            return self.message_output(db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())

    def inbox_count(self, identifier):
        with self.database() as db:
            row = db.execute("""SELECT COUNT(*), COALESCE(SUM(acknowledged_at IS NULL), 0)
                FROM messages WHERE to_agent_id = ?""", (identifier,)).fetchone()
        return {"total": row[0], "pending": row[1]}

    def inbox(self, identifier, unread_only, after_id, limit):
        unread = " AND acknowledged_at IS NULL" if unread_only else ""
        with self.database() as db:
            return [self.message_output(row) for row in db.execute(
                "SELECT * FROM messages WHERE to_agent_id = ? AND id > ?" + unread + " ORDER BY id LIMIT ?",
                (identifier, after_id, limit))]

    def get_message(self, identifier, message_id):
        with self.database() as db:
            row = db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None or identifier not in (row["from_agent_id"], row["to_agent_id"]):
                raise HTTPException(404, "Message not found in your conversations")
            return self.message_output(row)

    def _delete_message_ids(self, message_ids):
        if not message_ids:
            return 0, 0
        placeholders = ",".join("?" for _ in message_ids)
        with self.database() as db:
            unlinked = db.execute(
                f"UPDATE messages SET in_reply_to = NULL WHERE in_reply_to IN ({placeholders})",
                tuple(message_ids)).rowcount
            deleted = db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})",
                                 tuple(message_ids)).rowcount
        return deleted, unlinked

    def delete_message(self, identifier, message_id, admin=False):
        with self.database() as db:
            row = db.execute("SELECT from_agent_id, to_agent_id FROM messages WHERE id = ?",
                             (message_id,)).fetchone()
            if row is None or (not admin and identifier not in (row["from_agent_id"], row["to_agent_id"])):
                raise HTTPException(404, "Message not found in your conversations")
        deleted, unlinked = self._delete_message_ids([message_id])
        return {"id": message_id, "deleted": bool(deleted), "related_replies_unlinked": unlinked}

    def message_in_workspace(self, message_id, workspace_id):
        with self.database() as db:
            row = db.execute("SELECT from_agent_id,to_agent_id FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                return False
            placeholders = "?"
            for identifier in (row["from_agent_id"], row["to_agent_id"]):
                agent = db.execute("SELECT workspace_id FROM agents WHERE id=?", (identifier,)).fetchone()
                if agent and (agent["workspace_id"] or DEFAULT_WORKSPACE_ID) == workspace_id:
                    return True
        return False

    def message_for_workspace(self, message_id, workspace_id):
        """One full message when it belongs to this workspace, otherwise None."""
        if not self.message_in_workspace(message_id, workspace_id):
            return None
        with self.database() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return self.message_output(row) if row else None

    def dashboard_thread(self, thread_id, limit=50):
        """Every message of one thread, oldest first, for the dashboard timeline."""
        with self.database() as db:
            rows = db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id LIMIT ?",
                              (thread_id, limit)).fetchall()
        return [self.message_output(row) for row in rows]

    def delete_inbox(self, identifier, after_id, before_id, limit):
        clauses, params = ["to_agent_id = ?", "id > ?"], [identifier, after_id]
        if before_id is not None:
            clauses.append("id <= ?")
            params.append(before_id)
        with self.database() as db:
            rows = db.execute(f"SELECT id FROM messages WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?",
                              (*params, limit + 1)).fetchall()
        selected = [row["id"] for row in rows[:limit]]
        deleted, unlinked = self._delete_message_ids(selected)
        return {"deleted_count": deleted, "related_replies_unlinked": unlinked,
                "has_more": len(rows) > limit, "next_after_id": selected[-1] if selected else after_id}

    def recent_messages(self, limit=15, agent_ids=None):
        with self.database() as db:
            if agent_ids is None:
                rows = db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
            elif not agent_ids:
                return []
            else:
                placeholders = ",".join("?" for _ in agent_ids)
                rows = db.execute(f"SELECT * FROM messages WHERE from_agent_id IN ({placeholders}) OR to_agent_id IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                                  (*agent_ids, *agent_ids, limit))
            return [self.message_output(row) for row in rows]

    def get_attachment(self, identifier, message_id, attachment_index):
        with self.database() as db:
            row = db.execute("SELECT from_agent_id, to_agent_id FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None or identifier not in (row["from_agent_id"], row["to_agent_id"]):
                raise HTTPException(404, "Attachment not found in your conversation")
            attachment = db.execute("""SELECT filename, media_type, size_bytes, data FROM attachments
                WHERE message_id = ? AND attachment_index = ?""", (message_id, attachment_index)).fetchone()
        if attachment is None:
            raise HTTPException(404, "Attachment not found")
        return dict(attachment)

    def acknowledge(self, identifier, message_id):
        with self.database() as db:
            cursor = db.execute("""UPDATE messages SET acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE id = ? AND to_agent_id = ?""", (time.time(), message_id, identifier))
            if cursor.rowcount == 0:
                raise HTTPException(404, "Message not found in your inbox")


class FirestoreStore(CollaborationStore):
    def __init__(self, project, database, credentials=None):
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
        self.firestore, self.FieldFilter = firestore, FieldFilter
        self.client = firestore.Client(project=project, database=database, credentials=credentials)
        self.agents = self.client.collection("agents")
        self.messages = self.client.collection("messages")
        self.attachment_chunks = self.client.collection("attachment_chunks")
        self.sequence = self.client.collection("meta").document("sequence")

    def initialize(self):
        self.ping()

    def ping(self):
        self.sequence.get(timeout=10)

    def where(self, query, field, operator, value):
        return query.where(filter=self.FieldFilter(field, operator, value))

    def count(self, query):
        return query.count().get(timeout=15)[0][0].value

    def authenticate(self, digest):
        rows = list(self.where(self.agents, "token_hash", "==", digest).limit(1).stream(timeout=15))
        if rows:
            rows[0].reference.update({"last_seen": time.time()}, timeout=15)
            return rows[0].id
        return None

    def authenticate_agent_api_key(self, digest):
        rows = list(self.where(self.client.collection("agent_api_keys"), "token_hash", "==", digest)
                    .limit(1).stream(timeout=15))
        if not rows:
            return None
        row = rows[0].to_dict()
        if row.get("revoked_at") is not None:
            return None
        agent_ref = self.agents.document(row["agent_id"])
        agent = agent_ref.get(timeout=15).to_dict()
        if not agent or row.get("credential_epoch", 0) != agent.get("credential_epoch", 0):
            return None
        seen = time.time()
        rows[0].reference.update({"last_used_at": seen}, timeout=15)
        agent_ref.update({"last_seen": seen}, timeout=15)
        return {"id": row["agent_id"], "api_key_id": rows[0].id}

    def create_agent_api_key(self, row, expected_instance_hash):
        agent_ref = self.agents.document(row["agent_id"])
        key_ref = self.client.collection("agent_api_keys").document(row["id"])
        @self.firestore.transactional
        def create(transaction):
            agent = agent_ref.get(transaction=transaction).to_dict()
            if not agent or not secrets.compare_digest(agent.get("token_hash", ""), expected_instance_hash):
                raise HTTPException(401, "Instance credential changed; sign in again")
            transaction.create(key_ref, row | {"credential_epoch": agent.get("credential_epoch", 0)})
            transaction.update(agent_ref, {"api_key_revision": agent.get("api_key_revision", 0) + 1})
        create(self.client.transaction(max_attempts=5))
        return row

    def agent_api_keys(self, agent_id):
        rows = list(self.where(self.client.collection("agent_api_keys"), "agent_id", "==", agent_id)
                    .stream(timeout=15))
        agent = self.agents.document(agent_id).get(timeout=15).to_dict() or {}
        current_epoch = agent.get("credential_epoch", 0)
        result = []
        for doc in rows:
            row = doc.to_dict()
            if row.get("credential_epoch", 0) != current_epoch and row.get("revoked_at") is None:
                row["revoked_at"] = agent.get("credential_rotated_at") or time.time()
            result.append(row)
        return sorted(result, key=lambda row: row["created_at"], reverse=True)

    def rotate_agent_token(self, agent_id, token_hash, recovery_hash, expected_epoch, now):
        ref = self.agents.document(agent_id)
        @self.firestore.transactional
        def rotate(transaction):
            agent = ref.get(transaction=transaction).to_dict()
            if not agent:
                raise HTTPException(404, "Agent not found")
            current_epoch = agent.get("credential_epoch", 0)
            if agent.get("token_hash") == token_hash and agent.get("recovery_hash") == recovery_hash:
                if agent.get("last_rotation_hash") == recovery_hash and current_epoch == expected_epoch + 1:
                    return False
                raise HTTPException(409, "Choose a new recovery secret for token rotation")
            if current_epoch != expected_epoch:
                raise HTTPException(409, "Instance credential changed; refresh before rotating")
            query = self.where(self.client.collection("agent_api_keys"), "agent_id", "==", agent_id)
            active_keys = [doc for doc in query.stream(transaction=transaction, timeout=15)
                           if doc.to_dict().get("revoked_at") is None]
            if len(active_keys) > 400:
                raise HTTPException(409, "Too many active API keys to rotate atomically; revoke keys first")
            for doc in active_keys:
                transaction.update(doc.reference, {"revoked_at": now})
            transaction.update(ref, {"token_hash": token_hash,
                                     "credential_epoch": agent.get("credential_epoch", 0) + 1,
                                     "credential_rotated_at": now,
                                     "recovery_hash": recovery_hash,
                                     "last_rotation_hash": recovery_hash,
                                     "api_key_revision": agent.get("api_key_revision", 0) + 1})
            return True
        return rotate(self.client.transaction(max_attempts=5))

    def revoke_agent_api_key(self, agent_id, key_id):
        ref = self.client.collection("agent_api_keys").document(key_id)
        snapshot = ref.get(timeout=15)
        if not snapshot.exists:
            return False
        row = snapshot.to_dict()
        if row.get("agent_id") != agent_id or row.get("revoked_at") is not None:
            return False
        ref.update({"revoked_at": time.time()}, timeout=15)
        return True

    def register(self, row):
        from google.api_core.exceptions import AlreadyExists
        try:
            self.agents.document(row["id"]).create(row, timeout=15)
        except AlreadyExists:
            return False
        return True

    def update_agent_card(self, identifier, fields):
        ref = self.agents.document(identifier)
        snapshot = ref.get(timeout=15)
        if not snapshot.exists:
            raise HTTPException(404, "Agent not found")
        if fields.get("name") and snapshot.to_dict().get("profile_id"):
            fields = {**fields, "instance_name": fields["name"]}
        ref.update(fields, timeout=15)
        return {**snapshot.to_dict(), **fields}

    def report_listener(self, identifier, seen_at, ttl, wake_configured):
        self.agents.document(identifier).update({"listener_seen_at": seen_at, "listener_ttl": ttl,
                                                 "wake_configured": wake_configured}, timeout=15)

    def all_agents(self):
        rows = [doc.to_dict() | {"workspace_id": doc.to_dict().get("workspace_id") or DEFAULT_WORKSPACE_ID}
                for doc in self.agents.stream(timeout=15)]
        return sorted(rows, key=lambda row: (row["created_at"], row["id"]))

    def get_agent(self, identifier):
        row = self.agents.document(identifier).get(timeout=15).to_dict()
        return row | {"workspace_id": row.get("workspace_id") or DEFAULT_WORKSPACE_ID} if row else None

    def unregister(self, identifier):
        """Revoke an agent and permanently erase its messages and attachments."""
        agent_ref = self.agents.document(identifier)
        if not agent_ref.get(timeout=15).exists:
            return False
        message_ids = set()
        for field in ("from_agent_id", "to_agent_id"):
            query = self.where(self.messages, field, "==", identifier)
            message_ids.update(int(doc.id) for doc in query.stream(timeout=30))
        # Firestore batches are limited; delete attachment chunks before messages.
        refs = []
        refs.extend(doc.reference for doc in self.where(self.client.collection("delivery_keys"), "sender_id", "==", identifier).stream(timeout=30))
        refs.extend(doc.reference for doc in self.where(self.client.collection("agent_api_keys"), "agent_id", "==", identifier).stream(timeout=30))
        for message_id in message_ids:
            chunks = self.where(self.attachment_chunks, "message_id", "==", message_id)
            refs.extend(doc.reference for doc in chunks.stream(timeout=30))
            refs.append(self.message_reference(message_id))
        batch = self.client.batch()
        count = 0
        for ref in refs:
            batch.delete(ref)
            count += 1
            if count == 450:
                batch.commit(timeout=30)
                batch = self.client.batch()
                count = 0
        if count:
            batch.commit(timeout=30)
        agent_ref.delete(timeout=15)
        return True

    def stats(self, cutoff):
        return dict(agents_total=self.count(self.agents),
                    agents_online=self.count(self.where(self.agents, "last_seen", ">=", cutoff)),
                    messages_total=self.count(self.messages),
                    messages_pending=self.count(self.where(self.messages, "acknowledged_at", "==", None)))

    def message_reference(self, identifier):
        return self.messages.document(f"{identifier:020d}")

    def send(self, sender, body, attachments=None, finalize=None):
        attachments = attachments or []
        key_hash, payload_hash = delivery_identity(sender, body, attachments, finalize)
        @self.firestore.transactional
        def insert(transaction):
            key_ref = self.client.collection("delivery_keys").document(key_hash) if key_hash else None
            if key_ref:
                cached = key_ref.get(transaction=transaction).to_dict()
                if cached:
                    if cached["payload_hash"] != payload_hash:
                        raise HTTPException(409, "Idempotency key was used for different content")
                    previous = self.message_reference(cached["message_id"]).get(transaction=transaction).to_dict()
                    if previous is None:
                        raise HTTPException(410, "This delivery was deleted; do not retry it with this key")
                    return {**message_view(previous), "delivery_replayed": True}
            sender_snapshot = self.agents.document(sender).get(transaction=transaction)
            recipient = self.agents.document(body.to_agent_id).get(transaction=transaction)
            if not sender_snapshot.exists:
                raise HTTPException(401, "Sender instance is unavailable")
            if not recipient.exists:
                raise HTTPException(404, "Recipient not found")
            sender_workspace = sender_snapshot.to_dict().get("workspace_id") or DEFAULT_WORKSPACE_ID
            recipient_workspace = recipient.to_dict().get("workspace_id") or DEFAULT_WORKSPACE_ID
            if sender_workspace != recipient_workspace:
                connection_ref = self.client.collection("workspace_connections").document(
                    workspace_connection_id(sender_workspace, recipient_workspace))
                connection = connection_ref.get(transaction=transaction)
                if not connection.exists or connection.to_dict().get("status") != "accepted":
                    raise HTTPException(403, "These workspaces are not connected")
            original = None
            if body.in_reply_to is not None:
                original = self.message_reference(body.in_reply_to).get(transaction=transaction).to_dict()
                check_reply(original, sender, body.to_agent_id)
                if finalize and original.get("status", "queued") in ("completed", "cancelled"):
                    raise HTTPException(409, "Request is already closed")
            sequence = self.sequence.get(transaction=transaction).to_dict() or {}
            identifier = sequence.get("value", 0) + 1
            metadata = []
            for index, item in enumerate(attachments):
                chunk_size = 512 * 1024
                chunks = (len(item["data"]) + chunk_size - 1) // chunk_size
                metadata.append({key: item[key] for key in ("filename", "media_type", "size_bytes")} |
                                {"index": index, "chunks": chunks})
            row = dict(id=identifier, from_agent_id=sender, to_agent_id=body.to_agent_id,
                       text=body.text, in_reply_to=body.in_reply_to, created_at=time.time(), acknowledged_at=None,
                       attachments=metadata, **message_fields(body, identifier, original))
            transaction.set(self.sequence, {"value": identifier})
            transaction.create(self.message_reference(identifier), row)
            if key_ref:
                transaction.create(key_ref, {"sender_id": sender, "payload_hash": payload_hash,
                                             "message_id": identifier, "created_at": time.time()})
            if finalize:
                fields = {"status": finalize["status"], "status_note": "", "updated_at": time.time()}
                if finalize["status"] == "completed":
                    fields["acknowledged_at"] = original.get("acknowledged_at") or time.time()
                transaction.update(self.message_reference(body.in_reply_to), fields)
            for index, item in enumerate(attachments):
                chunk_size = 512 * 1024
                for chunk_index, offset in enumerate(range(0, len(item["data"]), chunk_size)):
                    key = f"{identifier:020d}_{index:04d}_{chunk_index:04d}"
                    transaction.create(self.attachment_chunks.document(key), {
                        "message_id": identifier, "attachment_index": index, "chunk_index": chunk_index,
                        "data": item["data"][offset:offset + chunk_size],
                    })
            return message_view(row)
        return insert(self.client.transaction(max_attempts=10))

    def inbox_count(self, identifier):
        query = self.where(self.messages, "to_agent_id", "==", identifier)
        return {"total": self.count(query), "pending": self.count(self.where(query, "acknowledged_at", "==", None))}

    def inbox(self, identifier, unread_only, after_id, limit):
        query = self.where(self.messages, "to_agent_id", "==", identifier)
        query = self.where(query, "id", ">", after_id)
        if unread_only:
            query = self.where(query, "acknowledged_at", "==", None)
        return [message_view(doc.to_dict()) for doc in query.order_by("id").limit(limit).stream(timeout=15)]

    def get_message(self, identifier, message_id):
        row = self.message_reference(message_id).get(timeout=15).to_dict()
        if row is None or identifier not in (row["from_agent_id"], row["to_agent_id"]):
            raise HTTPException(404, "Message not found in your conversations")
        return message_view(row)

    def _delete_message_ids(self, message_ids):
        if not message_ids:
            return 0, 0
        operations = []
        unlinked = 0
        for message_id in message_ids:
            replies = self.where(self.messages, "in_reply_to", "==", message_id).stream(timeout=30)
            for reply in replies:
                operations.append(("update", reply.reference, {"in_reply_to": None}))
                unlinked += 1
            chunks = self.where(self.attachment_chunks, "message_id", "==", message_id).stream(timeout=30)
            operations.extend(("delete", chunk.reference, None) for chunk in chunks)
            operations.append(("delete", self.message_reference(message_id), None))
        batch = self.client.batch()
        count = 0
        for operation, reference, fields in operations:
            if operation == "update":
                batch.update(reference, fields)
            else:
                batch.delete(reference)
            count += 1
            if count == 450:
                batch.commit(timeout=30)
                batch = self.client.batch()
                count = 0
        if count:
            batch.commit(timeout=30)
        return len(message_ids), unlinked

    def delete_message(self, identifier, message_id, admin=False):
        row = self.message_reference(message_id).get(timeout=15).to_dict()
        if row is None or (not admin and identifier not in (row["from_agent_id"], row["to_agent_id"])):
            raise HTTPException(404, "Message not found in your conversations")
        deleted, unlinked = self._delete_message_ids([message_id])
        return {"id": message_id, "deleted": bool(deleted), "related_replies_unlinked": unlinked}

    def message_in_workspace(self, message_id, workspace_id):
        row = self.message_reference(message_id).get(timeout=15).to_dict()
        if not row:
            return False
        return any(self.workspace_for_agent(identifier) == workspace_id
                   for identifier in (row["from_agent_id"], row["to_agent_id"]))

    def message_for_workspace(self, message_id, workspace_id):
        """One full message when it belongs to this workspace, otherwise None."""
        if not self.message_in_workspace(message_id, workspace_id):
            return None
        row = self.message_reference(message_id).get(timeout=15).to_dict()
        return message_view(row) if row else None

    def dashboard_thread(self, thread_id, limit=50):
        """Every message of one thread, oldest first, for the dashboard timeline."""
        query = self.where(self.messages, "thread_id", "==", thread_id).order_by("id").limit(limit)
        return [message_view(doc.to_dict()) for doc in query.stream(timeout=30)]

    def delete_inbox(self, identifier, after_id, before_id, limit):
        query = self.where(self.messages, "to_agent_id", "==", identifier)
        query = self.where(query, "id", ">", after_id)
        if before_id is not None:
            query = self.where(query, "id", "<=", before_id)
        rows = list(query.order_by("id").limit(limit + 1).stream(timeout=30))
        selected = [int(row.id) for row in rows[:limit]]
        deleted, unlinked = self._delete_message_ids(selected)
        return {"deleted_count": deleted, "related_replies_unlinked": unlinked,
                "has_more": len(rows) > limit, "next_after_id": selected[-1] if selected else after_id}

    def recent_messages(self, limit=15, agent_ids=None):
        if agent_ids is None:
            docs = self.messages.order_by("id", direction=self.firestore.Query.DESCENDING).limit(limit).stream(timeout=15)
            return [message_view(doc.to_dict()) for doc in docs]
        if not agent_ids:
            return []
        found = {}
        for identifier in agent_ids:
            for field in ("from_agent_id", "to_agent_id"):
                query = self.where(self.messages, field, "==", identifier).order_by(
                    "id", direction=self.firestore.Query.DESCENDING).limit(limit)
                for doc in query.stream(timeout=15):
                    found[doc.id] = doc.to_dict()
        rows = sorted(found.values(), key=lambda row: row["id"], reverse=True)[:limit]
        return [message_view(row) for row in rows]

    def get_attachment(self, identifier, message_id, attachment_index):
        row = self.message_reference(message_id).get(timeout=15).to_dict()
        if row is None or identifier not in (row["from_agent_id"], row["to_agent_id"]):
            raise HTTPException(404, "Attachment not found in your conversation")
        metadata = next((item for item in row.get("attachments", []) if item["index"] == attachment_index), None)
        if metadata is None:
            raise HTTPException(404, "Attachment not found")
        data = bytearray()
        for chunk_index in range(metadata["chunks"]):
            key = f"{message_id:020d}_{attachment_index:04d}_{chunk_index:04d}"
            chunk = self.attachment_chunks.document(key).get(timeout=15).to_dict()
            if chunk is None:
                raise HTTPException(500, "Attachment data is incomplete")
            data.extend(chunk["data"])
        return {"filename": metadata["filename"], "media_type": metadata["media_type"],
                "size_bytes": metadata["size_bytes"], "data": bytes(data)}

    def acknowledge(self, identifier, message_id):
        @self.firestore.transactional
        def acknowledge(transaction):
            reference = self.message_reference(message_id)
            row = reference.get(transaction=transaction).to_dict()
            if row is None or row["to_agent_id"] != identifier:
                raise HTTPException(404, "Message not found in your inbox")
            if row["acknowledged_at"] is None:
                transaction.update(reference, {"acknowledged_at": time.time()})
        acknowledge(self.client.transaction(max_attempts=10))

