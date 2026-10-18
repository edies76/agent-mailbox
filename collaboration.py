"""Profiles group independent mailboxes; request metadata never widens body access."""

import hashlib
import json
import secrets
import time
import threading
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

STATES = {"queued", "in_progress", "completed", "blocked", "cancelled"}


def iso_date(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def message_view(row):
    row = dict(row)
    row.setdefault("subject", "")
    row["thread_id"] = row.get("thread_id") or row.get("in_reply_to") or row["id"]
    row.setdefault("status", "queued")
    row.setdefault("message_type", "reply" if row.get("in_reply_to") else "request")
    if row["message_type"] == "reply" and row["status"] == "queued":
        row["status"] = "completed"
    row.setdefault("status_note", "")
    row.setdefault("share_subject", False)
    row["share_subject"] = bool(row["share_subject"])
    row["created_at_iso"] = iso_date(row.get("created_at"))
    row["updated_at_iso"] = iso_date(row.get("updated_at") or row.get("created_at"))
    files = row.get("attachments", [])
    row["attachment_count"] = len(files)
    row["has_images"] = any(item["media_type"].startswith("image/") for item in files)
    row["has_files"] = any(not item["media_type"].startswith("image/") for item in files)
    binding = [row["id"], row["from_agent_id"], row["to_agent_id"], row["subject"], row["text"]]
    row["request_fingerprint"] = hashlib.sha256(json.dumps(binding, ensure_ascii=False).encode()).hexdigest()
    return row


def message_fields(body, identifier, original=None):
    return dict(subject=body.subject or (original or {}).get("subject", ""),
                thread_id=(original or {}).get("thread_id") or (original or {}).get("id") or identifier,
                message_type="reply" if original else body.message_type,
                status="completed" if original or body.message_type == "note" else "queued", status_note="", updated_at=time.time(),
                share_subject=body.share_subject)


class CollaborationStore:
    def initialize_collaboration(self):
        if not hasattr(self, "database"):
            return
        with self.database() as db:
            additions = {
                "agents": {"profile_id": "TEXT", "instance_name": "TEXT NOT NULL DEFAULT ''"},
                "messages": {"subject": "TEXT NOT NULL DEFAULT ''", "thread_id": "INTEGER",
                             "message_type": "TEXT NOT NULL DEFAULT 'request'",
                             "status": "TEXT NOT NULL DEFAULT 'queued'", "status_note": "TEXT NOT NULL DEFAULT ''",
                             "updated_at": "REAL", "share_subject": "INTEGER NOT NULL DEFAULT 0"},
            }
            for table, columns in additions.items():
                present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, declaration in columns.items():
                    if column not in present:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            db.execute("CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS delivery_keys (key_hash TEXT PRIMARY KEY, sender_id TEXT NOT NULL, "
                       "payload_hash TEXT NOT NULL, message_id INTEGER NOT NULL, created_at REAL NOT NULL)")
            db.execute("UPDATE messages SET message_type = 'reply' WHERE in_reply_to IS NOT NULL AND message_type = 'request'")
            db.execute("CREATE INDEX IF NOT EXISTS outbox_idx ON messages(from_agent_id, id)")
            db.execute("CREATE INDEX IF NOT EXISTS thread_idx ON messages(thread_id, id)")
            # Old records remain addressable; follow their reply chain in ID order.
            for row in db.execute("SELECT id, in_reply_to FROM messages WHERE thread_id IS NULL ORDER BY id").fetchall():
                parent = db.execute("SELECT thread_id FROM messages WHERE id = ?", (row["in_reply_to"],)).fetchone()
                root = parent[0] if parent and parent[0] else row["id"]
                db.execute("UPDATE messages SET thread_id = ? WHERE id = ?", (root, row["id"]))

    def profiles_all(self):
        if hasattr(self, "database"):
            with self.database() as db:
                return [json.loads(row[0]) for row in db.execute("SELECT data_json FROM profiles")]
        return [doc.to_dict() for doc in self.client.collection("profiles").stream(timeout=15)]

    def profile_get(self, profile_id):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT data_json FROM profiles WHERE id = ?", (profile_id,)).fetchone()
                return json.loads(row[0]) if row else None
        return self.client.collection("profiles").document(profile_id).get(timeout=15).to_dict()

    def create_profile(self, identifier, name, kind, project):
        token = secrets.token_urlsafe(32)
        profile = dict(id=str(uuid4()), name=name, kind=kind, project=project, created_at=time.time(),
                       enrollment_hash=hashlib.sha256(token.encode()).hexdigest())
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                agent = db.execute("SELECT * FROM agents WHERE id = ?", (identifier,)).fetchone()
                if not agent or agent["profile_id"]:
                    raise HTTPException(409, "Instance is already grouped or no longer exists")
                db.execute("INSERT INTO profiles VALUES (?, ?)", (profile["id"], json.dumps(profile)))
                db.execute("UPDATE agents SET profile_id = ?, instance_name = name WHERE id = ?",
                           (profile["id"], identifier))
        else:
            @self.firestore.transactional
            def create(transaction):
                ref = self.agents.document(identifier)
                agent = ref.get(transaction=transaction).to_dict()
                if not agent or agent.get("profile_id"):
                    raise HTTPException(409, "Instance is already grouped or no longer exists")
                transaction.create(self.client.collection("profiles").document(profile["id"]), profile)
                transaction.update(ref, {"profile_id": profile["id"], "instance_name": agent["name"]})
            create(self.client.transaction(max_attempts=5))
        return profile, token

    def authorize_profile(self, profile_id, token):
        profile = self.profile_get(profile_id)
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not profile or not secrets.compare_digest(profile["enrollment_hash"], digest):
            raise HTTPException(401, "Invalid profile enrollment token")
        return profile

    def delete_empty_profile(self, profile_id):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM agents WHERE profile_id = ? LIMIT 1", (profile_id,)).fetchone():
                    raise HTTPException(409, "Retire all instances before deleting this profile")
                db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        else:
            @self.firestore.transactional
            def remove(transaction):
                ref = self.client.collection("profiles").document(profile_id)
                ref.get(transaction=transaction)
                members = self.where(self.agents, "profile_id", "==", profile_id).limit(1)
                if list(members.stream(transaction=transaction, timeout=15)):
                    raise HTTPException(409, "Retire all instances before deleting this profile")
                transaction.delete(ref)
            remove(self.client.transaction(max_attempts=5))

    def enroll_profile_instance(self, profile_id, row):
        row = {**row, "profile_id": profile_id, "instance_name": row["name"]}
        def verify(existing):
            if existing and (existing.get("profile_id") != profile_id or existing["token_hash"] != row["token_hash"]):
                raise HTTPException(409, "Native session identity conflict")
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone():
                    raise HTTPException(404, "Profile no longer exists")
                old = db.execute("SELECT * FROM agents WHERE id = ?", (row["id"],)).fetchone()
                verify(dict(old) if old else None)
                if old:
                    return False
                columns = ("id", "name", "kind", "project", "token_hash", "created_at", "last_seen", "profile_id", "instance_name")
                db.execute(f"INSERT INTO agents ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                           tuple(row[key] for key in columns))
            return True
        @self.firestore.transactional
        def enroll(transaction):
            profile_ref = self.client.collection("profiles").document(profile_id)
            profile = profile_ref.get(transaction=transaction).to_dict()
            instance_ref = self.agents.document(row["id"])
            old = instance_ref.get(transaction=transaction).to_dict()
            if not profile:
                raise HTTPException(404, "Profile no longer exists")
            verify(old)
            if old:
                return False
            transaction.create(instance_ref, row)
            transaction.update(profile_ref, {"membership_revision": profile.get("membership_revision", 0) + 1})
            return True
        return enroll(self.client.transaction(max_attempts=5))

    def attach_profile(self, identifier, profile_id, instance_name):
        fields = {"profile_id": profile_id, "instance_name": instance_name}
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("UPDATE agents SET profile_id = ?, instance_name = ? WHERE id = ?",
                           (profile_id, instance_name, identifier))
        else:
            self.agents.document(identifier).update(fields, timeout=15)

    def profile_activity(self, identifier, profile_id, admin=False):
        actor = self.get_agent(identifier) if identifier else None
        if not admin and (not actor or actor.get("profile_id") != profile_id):
            raise HTTPException(403, "Only profile members can see its activity metadata")
        if not self.profile_get(profile_id):
            raise HTTPException(404, "Profile not found")
        instances = []
        for agent in self.all_agents():
            if agent.get("profile_id") != profile_id:
                continue
            counts = self.inbox_count(agent["id"])
            # Count is shared inside the profile. Subjects are opt-in, never bodies/file names.
            rows = self.mail_page(agent["id"], "inbox", 0, 100, unread_only=True)["messages"]
            subjects = [{"id": row["id"], "subject": row["subject"],
                         "created_at_iso": row["created_at_iso"]} for row in rows if row["share_subject"]]
            instances.append({"instance_id": agent["id"], "instance_name": agent.get("instance_name") or agent["name"],
                              "pending": counts["pending"], "total": counts["total"], "shared_subjects": subjects})
        return {"profile_id": profile_id, "instances": instances,
                "pending_total": sum(row["pending"] for row in instances),
                "visibility": "counts and explicitly shared subjects only"}

    def mail_page(self, identifier, box, after_id=0, limit=50, status=None, thread_id=None,
                  has_attachments=None, unread_only=False):
        field = "to_agent_id" if box == "inbox" else "from_agent_id"
        if hasattr(self, "database"):
            with self.database() as db:
                unread_clause = " AND acknowledged_at IS NULL" if unread_only else ""
                rows = [self.message_output(row) for row in db.execute(
                    f"SELECT * FROM messages WHERE {field} = ? AND id > ?{unread_clause} ORDER BY id LIMIT 1001",
                    (identifier, after_id))]
        else:
            query = self.where(self.messages, field, "==", identifier)
            query = self.where(query, "id", ">", after_id)
            if unread_only:
                query = self.where(query, "acknowledged_at", "==", None)
            rows = [message_view(doc.to_dict()) for doc in query.order_by("id").limit(1001).stream(timeout=20)]
        found, cursor, scanned = [], after_id, 0
        for row in rows[:1000]:
            cursor, scanned = row["id"], scanned + 1
            if unread_only and row.get("acknowledged_at") is not None:
                continue
            if status and row["status"] != status:
                continue
            if thread_id and row["thread_id"] != thread_id:
                continue
            if has_attachments is not None and bool(row["attachments"]) != has_attachments:
                continue
            found.append(row)
            if len(found) == limit:
                break
        return {"messages": found, "has_more": len(rows) > scanned, "next_after_id": cursor,
                "scanned": scanned, "order": "oldest_first", "timezone": "UTC"}

    def wait_for_mail(self, identifier, after_id, timeout):
        started = time.monotonic()
        if hasattr(self, "database"):
            deadline = started + timeout
            while True:
                rows = self.inbox(identifier, True, after_id, 1)
                if rows:
                    with self.database() as db:
                        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'messages'").fetchone()
                    return {"changed": True, "message_id": rows[0]["id"], "after_id": after_id,
                            "event_cursor": sequence[0] if sequence else rows[0]["id"],
                            "waited_ms": round((time.monotonic() - started) * 1000)}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"changed": False, "after_id": after_id, "waited_ms": round(timeout * 1000)}
                time.sleep(min(.1, remaining))
        changed, connected = threading.Event(), threading.Event()
        first_id = []
        query = self.where(self.messages, "to_agent_id", "==", identifier)
        query = self.where(query, "acknowledged_at", "==", None)
        query = self.where(query, "id", ">", after_id).order_by("id").limit(1)
        def received(documents, changes, read_time):
            connected.set()
            if documents:
                first_id[:] = [documents[0].to_dict()["id"]]
                changed.set()
        watch = query.on_snapshot(received)
        try:
            changed.wait(timeout)
            if not connected.is_set():
                raise HTTPException(503, "Event delivery is temporarily unavailable; use normal inbox polling")
            cursor = (self.sequence.get(timeout=10).to_dict() or {}).get("value", after_id) if changed.is_set() else after_id
            return {"changed": changed.is_set(), "message_id": first_id[0] if first_id else None, "event_cursor": cursor,
                    "after_id": after_id, "waited_ms": round((time.monotonic() - started) * 1000)}
        finally:
            watch.unsubscribe()

    def update_status(self, identifier, message_id, status, note=""):
        if status not in STATES:
            raise HTTPException(422, "Unknown request status")
        fields = {"status": status, "status_note": note, "updated_at": time.time()}
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT * FROM messages WHERE id = ? AND to_agent_id = ?",
                                 (message_id, identifier)).fetchone()
                if not row:
                    raise HTTPException(404, "Request not found in your inbox")
                db.execute("UPDATE messages SET status = ?, status_note = ?, updated_at = ? WHERE id = ?",
                           (status, note, fields["updated_at"], message_id))
        else:
            @self.firestore.transactional
            def change(transaction):
                ref = self.message_reference(message_id)
                row = ref.get(transaction=transaction).to_dict()
                if not row or row["to_agent_id"] != identifier:
                    raise HTTPException(404, "Request not found in your inbox")
                transaction.update(ref, fields)
            change(self.client.transaction(max_attempts=5))
        return self.get_message(identifier, message_id)

    def thread_messages(self, identifier, message_id, after_id=0, limit=100):
        anchor = self.get_message(identifier, message_id)
        root = anchor["thread_id"]
        if hasattr(self, "database"):
            with self.database() as db:
                rows = [self.message_output(row) for row in db.execute(
                    "SELECT * FROM messages WHERE thread_id = ? AND id > ? AND "
                    "(from_agent_id = ? OR to_agent_id = ?) ORDER BY id LIMIT ?",
                    (root, after_id, identifier, identifier, limit + 1))]
        else:
            query = self.where(self.messages, "thread_id", "==", root)
            query = self.where(query, "id", ">", after_id)
            rows = [message_view(doc.to_dict()) for doc in query.order_by("id").limit(limit + 1).stream(timeout=20)]
            root_row = self.message_reference(root).get(timeout=15).to_dict()
            if root_row and root_row["id"] > after_id:
                rows.append(message_view(root_row))
            legacy = self.where(self.messages, "in_reply_to", "==", root).limit(limit + 1)
            rows.extend(message_view(doc.to_dict()) for doc in legacy.stream(timeout=20)
                        if doc.to_dict()["id"] > after_id)
            rows = [row for row in rows if identifier in (row["from_agent_id"], row["to_agent_id"])]
            rows = sorted({row["id"]: row for row in rows}.values(), key=lambda row: row["id"])
        return {"thread_id": root, "messages": rows[:limit], "has_more": len(rows) > limit,
                "next_after_id": rows[min(limit, len(rows)) - 1]["id"] if rows else after_id}
