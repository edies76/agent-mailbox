"""Persistent mailbox storage: local SQLite or a dedicated Firestore database."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import json

from fastapi import HTTPException


def check_reply(original, sender, recipient):
    if original is None or original["to_agent_id"] != sender or original["from_agent_id"] != recipient:
        raise HTTPException(404, "Original message not found in this conversation")


class SQLiteStore:
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
                    created_at REAL NOT NULL, last_seen REAL NOT NULL
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
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if "attachments_json" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
            db.execute("""CREATE TABLE IF NOT EXISTS attachments (
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                attachment_index INTEGER NOT NULL, filename TEXT NOT NULL, media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, data BLOB NOT NULL,
                PRIMARY KEY(message_id, attachment_index)
            )""")

    @staticmethod
    def message_output(row):
        result = dict(row)
        result["attachments"] = json.loads(result.pop("attachments_json", "[]"))
        return result

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

    def register(self, row):
        with self.database() as db:
            db.execute("INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?)",
                       tuple(row[k] for k in ("id", "name", "kind", "project", "token_hash", "created_at", "last_seen")))

    def all_agents(self):
        with self.database() as db:
            return [dict(row) for row in db.execute("SELECT * FROM agents ORDER BY created_at, id")]

    def get_agent(self, identifier):
        with self.database() as db:
            row = db.execute("SELECT * FROM agents WHERE id = ?", (identifier,)).fetchone()
        return dict(row) if row else None

    def unregister(self, identifier):
        """Revoke an agent and permanently erase its messages and attachments."""
        with self.database() as db:
            if db.execute("SELECT 1 FROM agents WHERE id = ?", (identifier,)).fetchone() is None:
                return False
            db.execute("DELETE FROM messages WHERE from_agent_id = ? OR to_agent_id = ?",
                       (identifier, identifier))
            db.execute("DELETE FROM agents WHERE id = ?", (identifier,))
        return True

    def stats(self, cutoff):
        with self.database() as db:
            agents = db.execute("SELECT COUNT(*), COALESCE(SUM(last_seen >= ?), 0) FROM agents", (cutoff,)).fetchone()
            messages = db.execute("SELECT COUNT(*), COALESCE(SUM(acknowledged_at IS NULL), 0) FROM messages").fetchone()
        return dict(agents_total=agents[0], agents_online=agents[1], messages_total=messages[0], messages_pending=messages[1])

    def send(self, sender, body, attachments=None):
        attachments = attachments or []
        metadata = [{key: item[key] for key in ("filename", "media_type", "size_bytes")} | {"index": i}
                    for i, item in enumerate(attachments)]
        with self.database() as db:
            if db.execute("SELECT 1 FROM agents WHERE id = ?", (body.to_agent_id,)).fetchone() is None:
                raise HTTPException(404, "Recipient not found")
            if body.in_reply_to is not None:
                original = db.execute("SELECT * FROM messages WHERE id = ?", (body.in_reply_to,)).fetchone()
                check_reply(original, sender, body.to_agent_id)
            cursor = db.execute("""INSERT INTO messages
                (from_agent_id, to_agent_id, text, in_reply_to, created_at, attachments_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (sender, body.to_agent_id, body.text, body.in_reply_to, time.time(),
                 json.dumps(metadata, ensure_ascii=False)))
            message_id = cursor.lastrowid
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

    def recent_messages(self, limit=15):
        with self.database() as db:
            return [self.message_output(row) for row in db.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))]

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


class FirestoreStore:
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

    def register(self, row):
        self.agents.document(row["id"]).create(row, timeout=15)

    def all_agents(self):
        rows = [doc.to_dict() for doc in self.agents.stream(timeout=15)]
        return sorted(rows, key=lambda row: (row["created_at"], row["id"]))

    def get_agent(self, identifier):
        return self.agents.document(identifier).get(timeout=15).to_dict()

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

    def send(self, sender, body, attachments=None):
        attachments = attachments or []
        @self.firestore.transactional
        def insert(transaction):
            recipient = self.agents.document(body.to_agent_id).get(transaction=transaction)
            if not recipient.exists:
                raise HTTPException(404, "Recipient not found")
            if body.in_reply_to is not None:
                original = self.message_reference(body.in_reply_to).get(transaction=transaction).to_dict()
                check_reply(original, sender, body.to_agent_id)
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
                       attachments=metadata)
            transaction.set(self.sequence, {"value": identifier})
            transaction.create(self.message_reference(identifier), row)
            for index, item in enumerate(attachments):
                chunk_size = 512 * 1024
                for chunk_index, offset in enumerate(range(0, len(item["data"]), chunk_size)):
                    key = f"{identifier:020d}_{index:04d}_{chunk_index:04d}"
                    transaction.create(self.attachment_chunks.document(key), {
                        "message_id": identifier, "attachment_index": index, "chunk_index": chunk_index,
                        "data": item["data"][offset:offset + chunk_size],
                    })
            return row
        return insert(self.client.transaction(max_attempts=10))

    def inbox_count(self, identifier):
        query = self.where(self.messages, "to_agent_id", "==", identifier)
        return {"total": self.count(query), "pending": self.count(self.where(query, "acknowledged_at", "==", None))}

    def inbox(self, identifier, unread_only, after_id, limit):
        query = self.where(self.messages, "to_agent_id", "==", identifier)
        query = self.where(query, "id", ">", after_id)
        if unread_only:
            query = self.where(query, "acknowledged_at", "==", None)
        return [doc.to_dict() for doc in query.order_by("id").limit(limit).stream(timeout=15)]

    def get_message(self, identifier, message_id):
        row = self.message_reference(message_id).get(timeout=15).to_dict()
        if row is None or identifier not in (row["from_agent_id"], row["to_agent_id"]):
            raise HTTPException(404, "Message not found in your conversations")
        return row

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

    def recent_messages(self, limit=15):
        return [doc.to_dict() for doc in self.messages.order_by(
            "id", direction=self.firestore.Query.DESCENDING).limit(limit).stream(timeout=15)]

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

