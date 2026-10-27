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
DEFAULT_WORKSPACE_ID = "workspace-default"


def workspace_connection_id(workspace_a, workspace_b):
    pair = "\0".join(sorted((workspace_a, workspace_b)))
    return hashlib.sha256(pair.encode()).hexdigest()


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
    row.setdefault("delivery_mode", "inbox")
    row["wake_backlog"] = bool(row.get("wake_backlog", False))
    row["created_at_iso"] = iso_date(row.get("created_at"))
    row["updated_at_iso"] = iso_date(row.get("updated_at") or row.get("created_at"))
    files = row.get("attachments", [])
    row["attachment_count"] = len(files)
    row["has_images"] = any(item["media_type"].startswith("image/") for item in files)
    row["has_files"] = any(not item["media_type"].startswith("image/") for item in files)
    binding = [row["id"], row["from_agent_id"], row["to_agent_id"], row["subject"], row["text"]]
    if row["delivery_mode"] != "inbox" or row["wake_backlog"]:
        binding.extend([row["delivery_mode"], row["wake_backlog"]])
    row["request_fingerprint"] = hashlib.sha256(json.dumps(binding, ensure_ascii=False).encode()).hexdigest()
    return row


def message_fields(body, identifier, original=None):
    return dict(subject=body.subject or (original or {}).get("subject", ""),
                thread_id=(original or {}).get("thread_id") or (original or {}).get("id") or identifier,
                message_type="reply" if original else body.message_type,
                delivery_mode="inbox" if original else body.delivery_mode,
                wake_backlog=False if original else body.wake_backlog,
                status="completed" if original or body.message_type == "note" else "queued", status_note="", updated_at=time.time(),
                share_subject=body.share_subject)


class CollaborationStore:
    def initialize_collaboration(self):
        if not hasattr(self, "database"):
            return
        with self.database() as db:
            additions = {
                "agents": {"profile_id": "TEXT", "instance_name": "TEXT NOT NULL DEFAULT ''",
                           "description": "TEXT NOT NULL DEFAULT ''",
                           "capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
                           "workspace_id": f"TEXT NOT NULL DEFAULT '{DEFAULT_WORKSPACE_ID}'",
                           "credential_epoch": "INTEGER NOT NULL DEFAULT 0",
                           "credential_rotated_at": "REAL", "recovery_hash": "TEXT", "last_rotation_hash": "TEXT"},
                "agent_api_keys": {"credential_epoch": "INTEGER NOT NULL DEFAULT 0"},
                "messages": {"subject": "TEXT NOT NULL DEFAULT ''", "thread_id": "INTEGER",
                             "message_type": "TEXT NOT NULL DEFAULT 'request'",
                             "delivery_mode": "TEXT NOT NULL DEFAULT 'inbox'",
                             "wake_backlog": "INTEGER NOT NULL DEFAULT 0",
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
            db.execute("CREATE TABLE IF NOT EXISTS control_users (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, "
                       "token_hash TEXT UNIQUE NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL, "
                       "last_seen REAL, revoked_at REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS control_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, "
                       "action TEXT NOT NULL, target_id TEXT NOT NULL, created_at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS control_user_hash_idx ON control_users(token_hash)")
            db.execute("""CREATE TABLE IF NOT EXISTS control_sessions (
                id TEXT PRIMARY KEY, control_user_id TEXT, identity_type TEXT NOT NULL,
                username TEXT NOT NULL, role TEXT NOT NULL, credential_hash TEXT,
                token_hash TEXT UNIQUE NOT NULL, csrf_hash TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, last_seen REAL NOT NULL,
                revoked_at REAL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS control_session_hash_idx ON control_sessions(token_hash)")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account_users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL, username_norm TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'owner',
                    created_at REAL NOT NULL, last_login_at REAL, disabled_at REAL,
                    workspace_id TEXT NOT NULL DEFAULT 'workspace-default'
                );
                CREATE TABLE IF NOT EXISTS account_registration_keys (
                    user_id TEXT PRIMARY KEY REFERENCES account_users(id) ON DELETE CASCADE,
                    workspace_id TEXT NOT NULL,
                    key_hash TEXT UNIQUE NOT NULL,
                    key_prefix TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    rotated_at REAL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS account_registration_key_hash_idx ON account_registration_keys(key_hash);
                CREATE TABLE IF NOT EXISTS account_sessions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES account_users(id) ON DELETE CASCADE,
                    token_hash TEXT UNIQUE NOT NULL, csrf_hash TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL NOT NULL, last_seen REAL NOT NULL, revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS account_session_hash_idx ON account_sessions(token_hash);
                CREATE TABLE IF NOT EXISTS account_login_limits (
                    key_hash TEXT PRIMARY KEY, window_started REAL NOT NULL, failures INTEGER NOT NULL,
                    locked_until REAL NOT NULL DEFAULT 0
                );
            """)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_invites (
                    id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, workspace_id TEXT NOT NULL,
                    created_by TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    used_at REAL, revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS workspace_invite_hash_idx ON workspace_invites(code_hash);
                CREATE TABLE IF NOT EXISTS workspace_connections (
                    id TEXT PRIMARY KEY, workspace_a TEXT NOT NULL, workspace_b TEXT NOT NULL,
                    status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
            """)
            account_columns = {row[1] for row in db.execute("PRAGMA table_info(account_users)")}
            if "workspace_id" not in account_columns:
                db.execute("ALTER TABLE account_users ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'workspace-default'")
            db.execute("INSERT OR IGNORE INTO workspaces (id,name,created_at) VALUES (?,?,?)",
                       (DEFAULT_WORKSPACE_ID, "Workspace principal", time.time()))
            db.execute("UPDATE agents SET workspace_id = ? WHERE workspace_id IS NULL OR workspace_id = ''",
                       (DEFAULT_WORKSPACE_ID,))
            db.execute("UPDATE account_users SET workspace_id = ? WHERE workspace_id IS NULL OR workspace_id = ''",
                       (DEFAULT_WORKSPACE_ID,))
            db.execute("UPDATE messages SET message_type = 'reply' WHERE in_reply_to IS NOT NULL AND message_type = 'request'")
            db.execute("CREATE INDEX IF NOT EXISTS outbox_idx ON messages(from_agent_id, id)")
            db.execute("CREATE INDEX IF NOT EXISTS thread_idx ON messages(thread_id, id)")
            # Old records remain addressable; follow their reply chain in ID order.
            for row in db.execute("SELECT id, in_reply_to FROM messages WHERE thread_id IS NULL ORDER BY id").fetchall():
                parent = db.execute("SELECT thread_id FROM messages WHERE id = ?", (row["in_reply_to"],)).fetchone()
                root = parent[0] if parent and parent[0] else row["id"]
                db.execute("UPDATE messages SET thread_id = ? WHERE id = ?", (root, row["id"]))

    def owner_account_exists(self):
        if hasattr(self, "database"):
            with self.database() as db:
                return db.execute("SELECT 1 FROM account_users WHERE role = 'owner' AND disabled_at IS NULL LIMIT 1").fetchone() is not None
        owner = self.client.collection("meta").document("account_owner").get(timeout=15)
        return owner.exists

    def ensure_default_workspace(self):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("INSERT OR IGNORE INTO workspaces (id,name,created_at) VALUES (?,?,?)",
                           (DEFAULT_WORKSPACE_ID, "Workspace principal", time.time()))
            return
        marker = self.client.collection("meta").document("workspace_migration_v1")
        if marker.get(timeout=15).exists:
            return
        now = time.time()
        workspace_ref = self.client.collection("workspaces").document(DEFAULT_WORKSPACE_ID)
        if not workspace_ref.get(timeout=15).exists:
            workspace_ref.set({"id": DEFAULT_WORKSPACE_ID, "name": "Workspace principal", "created_at": now})
        for collection_name in ("agents", "account_users"):
            batch, queued = self.client.batch(), 0
            for doc in self.client.collection(collection_name).stream(timeout=30):
                if not doc.to_dict().get("workspace_id"):
                    batch.update(doc.reference, {"workspace_id": DEFAULT_WORKSPACE_ID})
                    queued += 1
                    if queued == 450:
                        batch.commit(timeout=30)
                        batch, queued = self.client.batch(), 0
            if queued:
                batch.commit(timeout=30)
        marker.set({"completed_at": now}, timeout=15)

    def workspace_for_agent(self, agent_id):
        row = self.get_agent(agent_id)
        return (row or {}).get("workspace_id") or DEFAULT_WORKSPACE_ID

    def workspace_info(self, workspace_id):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT id,name,created_at FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
                return dict(row) if row else None
        snapshot = self.client.collection("workspaces").document(workspace_id).get(timeout=15)
        return snapshot.to_dict() if snapshot.exists else None

    def workspace_stats(self, workspace_id, cutoff, all_agents=None):
        agents = [row for row in (self.all_agents() if all_agents is None else all_agents)
                  if (row.get("workspace_id") or DEFAULT_WORKSPACE_ID) == workspace_id]
        counts = [self.inbox_count(row["id"]) for row in agents]
        return {"agents_total": len(agents),
                "agents_online": sum(row.get("last_seen", 0) >= cutoff for row in agents),
                "messages_total": sum(row["total"] for row in counts),
                "messages_pending": sum(row["pending"] for row in counts)}

    def connected_workspaces(self, workspace_id):
        if hasattr(self, "database"):
            with self.database() as db:
                rows = db.execute("SELECT workspace_a,workspace_b FROM workspace_connections "
                                  "WHERE status='accepted' AND (workspace_a=? OR workspace_b=?)",
                                  (workspace_id, workspace_id)).fetchall()
            return {row[1] if row[0] == workspace_id else row[0] for row in rows}
        collection = self.client.collection("workspace_connections")
        docs = list(self.where(collection, "workspace_a", "==", workspace_id).stream(timeout=15))
        docs += list(self.where(collection, "workspace_b", "==", workspace_id).stream(timeout=15))
        result = set()
        for doc in docs:
            row = doc.to_dict()
            if row.get("status") == "accepted":
                result.add(row["workspace_b"] if row["workspace_a"] == workspace_id else row["workspace_a"])
        return result

    def create_workspace_invite(self, row):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("INSERT INTO workspace_invites (id,code_hash,workspace_id,created_by,created_at,expires_at,used_at,revoked_at) "
                           "VALUES (?,?,?,?,?,?,NULL,NULL)",
                           tuple(row[key] for key in ("id","code_hash","workspace_id","created_by","created_at","expires_at")))
        else:
            self.client.collection("workspace_invites").document(row["id"]).create(row | {"used_at": None,"revoked_at": None}, timeout=15)
        return row

    def accept_workspace_invite(self, code_hash, user, workspace):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                invite = db.execute("SELECT * FROM workspace_invites WHERE code_hash=?", (code_hash,)).fetchone()
                now = user["created_at"]
                if not invite or invite["used_at"] is not None or invite["revoked_at"] is not None or invite["expires_at"] <= now:
                    raise HTTPException(410, "Invitation is invalid, expired or already used")
                if db.execute("SELECT 1 FROM account_users WHERE username_norm=?", (user["username_norm"],)).fetchone():
                    raise HTTPException(409, "Username is already taken")
                db.execute("INSERT INTO workspaces (id,name,created_at) VALUES (?,?,?)",
                           (workspace["id"], workspace["name"], workspace["created_at"]))
                db.execute("INSERT INTO account_users (id,username,username_norm,password_hash,role,created_at,last_login_at,disabled_at,workspace_id) "
                           "VALUES (?,?,?,?, 'workspace_owner',?,?,NULL,?)",
                           (user["id"], user["username"], user["username_norm"], user["password_hash"],
                            user["created_at"], user["last_login_at"], workspace["id"]))
                a, b = sorted((invite["workspace_id"], workspace["id"]))
                key = workspace_connection_id(a, b)
                db.execute("INSERT INTO workspace_connections (id,workspace_a,workspace_b,status,created_at,updated_at) "
                           "VALUES (?,?,?,'accepted',?,?)", (key, a, b, now, now))
                db.execute("UPDATE workspace_invites SET used_at=? WHERE id=? AND used_at IS NULL", (now, invite["id"]))
            return user | {"workspace_id": workspace["id"], "role": "workspace_owner"}

        invite_query = self.where(self.client.collection("workspace_invites"), "code_hash", "==", code_hash).limit(1)
        matches = list(invite_query.stream(timeout=15))
        if not matches:
            raise HTTPException(410, "Invitation is invalid, expired or already used")
        invite_ref = matches[0].reference
        inviter_workspace = matches[0].to_dict()["workspace_id"]
        workspace_id = workspace["id"]
        a, b = sorted((inviter_workspace, workspace_id))
        workspace_ref = self.client.collection("workspaces").document(workspace_id)
        user_ref = self.client.collection("account_users").document(user["id"])
        username_ref = self.client.collection("account_usernames").document(user["username_norm"])
        connection_ref = self.client.collection("workspace_connections").document(workspace_connection_id(a, b))
        @self.firestore.transactional
        def create(transaction):
            invite_snapshot = invite_ref.get(transaction=transaction)
            username_snapshot = username_ref.get(transaction=transaction)
            if not invite_snapshot.exists:
                raise HTTPException(410, "Invitation is invalid, expired or already used")
            current = invite_snapshot.to_dict()
            now = user["created_at"]
            if current.get("used_at") is not None or current.get("revoked_at") is not None or current.get("expires_at",0) <= now:
                raise HTTPException(410, "Invitation is invalid, expired or already used")
            if username_snapshot.exists:
                raise HTTPException(409, "Username is already taken")
            transaction.create(workspace_ref, workspace)
            transaction.create(user_ref, user | {"role":"workspace_owner","workspace_id":workspace_id})
            transaction.create(username_ref, {"user_id":user["id"]})
            transaction.create(connection_ref, {"id":connection_ref.id,"workspace_a":a,"workspace_b":b,
                                                "status":"accepted","created_at":now,"updated_at":now})
            transaction.update(invite_ref, {"used_at":now})
        create(self.client.transaction(max_attempts=5))
        return user | {"workspace_id":workspace_id,"role":"workspace_owner"}

    def accept_existing_workspace_invite(self, code_hash, workspace_id, now):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                invite = db.execute("SELECT * FROM workspace_invites WHERE code_hash=?", (code_hash,)).fetchone()
                if not invite or invite["used_at"] is not None or invite["revoked_at"] is not None or invite["expires_at"] <= now:
                    raise HTTPException(410, "Invitation is invalid, expired or already used")
                inviter = invite["workspace_id"]
                if inviter == workspace_id:
                    raise HTTPException(409, "You cannot connect your workspace to itself")
                a, b = sorted((inviter, workspace_id))
                key = workspace_connection_id(a, b)
                current = db.execute("SELECT status FROM workspace_connections WHERE id=?", (key,)).fetchone()
                if current and current["status"] == "accepted":
                    raise HTTPException(409, "These workspaces are already connected")
                if current:
                    db.execute("UPDATE workspace_connections SET status='accepted',updated_at=? WHERE id=?", (now,key))
                else:
                    db.execute("INSERT INTO workspace_connections (id,workspace_a,workspace_b,status,created_at,updated_at) "
                               "VALUES (?,?,?,'accepted',?,?)", (key,a,b,now,now))
                db.execute("UPDATE workspace_invites SET used_at=? WHERE id=? AND used_at IS NULL", (now,invite["id"]))
            return {"workspace_id": inviter, "connected": True}
        matches = list(self.where(self.client.collection("workspace_invites"), "code_hash", "==", code_hash).limit(1).stream(timeout=15))
        if not matches:
            raise HTTPException(410, "Invitation is invalid, expired or already used")
        invite_ref = matches[0].reference
        inviter = matches[0].to_dict()["workspace_id"]
        if inviter == workspace_id:
            raise HTTPException(409, "You cannot connect your workspace to itself")
        a, b = sorted((inviter, workspace_id))
        connection_ref = self.client.collection("workspace_connections").document(workspace_connection_id(a,b))
        @self.firestore.transactional
        def connect(transaction):
            invite_snapshot = invite_ref.get(transaction=transaction)
            connection_snapshot = connection_ref.get(transaction=transaction)
            if not invite_snapshot.exists:
                raise HTTPException(410, "Invitation is invalid, expired or already used")
            row = invite_snapshot.to_dict()
            if row.get("used_at") is not None or row.get("revoked_at") is not None or row.get("expires_at",0) <= now:
                raise HTTPException(410, "Invitation is invalid, expired or already used")
            if connection_snapshot.exists and connection_snapshot.to_dict().get("status") == "accepted":
                raise HTTPException(409, "These workspaces are already connected")
            connection = {"id": connection_ref.id,"workspace_a":a,"workspace_b":b,
                          "status":"accepted","created_at":now,"updated_at":now}
            transaction.set(connection_ref, connection)
            transaction.update(invite_ref, {"used_at":now})
        connect(self.client.transaction(max_attempts=5))
        return {"workspace_id":inviter,"connected":True}

    def workspace_invites(self, workspace_id):
        if hasattr(self, "database"):
            with self.database() as db:
                rows = db.execute("SELECT id,workspace_id,created_by,created_at,expires_at,used_at,revoked_at "
                                  "FROM workspace_invites WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,)).fetchall()
                return [dict(row) for row in rows]
        docs = self.where(self.client.collection("workspace_invites"), "workspace_id", "==", workspace_id).stream(timeout=15)
        fields = ("id","workspace_id","created_by","created_at","expires_at","used_at","revoked_at")
        return sorted(({key: doc.to_dict().get(key) for key in fields} for doc in docs),
                      key=lambda row: row["created_at"], reverse=True)

    def revoke_workspace_invite(self, workspace_id, invite_id, now):
        if hasattr(self, "database"):
            with self.database() as db:
                result = db.execute("UPDATE workspace_invites SET revoked_at=? WHERE id=? AND workspace_id=? "
                                    "AND used_at IS NULL AND revoked_at IS NULL", (now,invite_id,workspace_id))
                return bool(result.rowcount)
        ref = self.client.collection("workspace_invites").document(invite_id)
        @self.firestore.transactional
        def revoke(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                return False
            row = snap.to_dict()
            if row.get("workspace_id") != workspace_id or row.get("used_at") is not None or row.get("revoked_at") is not None:
                return False
            transaction.update(ref, {"revoked_at": now})
            return True
        return revoke(self.client.transaction(max_attempts=5))

    def workspace_connections(self, workspace_id):
        if hasattr(self, "database"):
            with self.database() as db:
                rows = db.execute("SELECT id,workspace_a,workspace_b,status,created_at,updated_at FROM workspace_connections "
                                  "WHERE workspace_a=? OR workspace_b=? ORDER BY created_at DESC", (workspace_id,workspace_id)).fetchall()
                return [dict(row) for row in rows]
        coll = self.client.collection("workspace_connections")
        docs = list(self.where(coll,"workspace_a","==",workspace_id).stream(timeout=15))
        docs += list(self.where(coll,"workspace_b","==",workspace_id).stream(timeout=15))
        unique = {doc.id:doc.to_dict() for doc in docs}
        return sorted(unique.values(),key=lambda row:row.get("created_at",0),reverse=True)

    def disconnect_workspaces(self, workspace_id, peer_workspace_id, now):
        key = workspace_connection_id(workspace_id, peer_workspace_id)
        if hasattr(self, "database"):
            with self.database() as db:
                result = db.execute("UPDATE workspace_connections SET status='revoked',updated_at=? WHERE id=? "
                                    "AND status='accepted' AND (workspace_a=? OR workspace_b=?)",
                                    (now,key,workspace_id,workspace_id))
                return bool(result.rowcount)
        ref = self.client.collection("workspace_connections").document(key)
        @self.firestore.transactional
        def disconnect(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                return False
            row = snap.to_dict()
            if workspace_id not in (row.get("workspace_a"), row.get("workspace_b")) or row.get("status") != "accepted":
                return False
            transaction.update(ref, {"status": "revoked", "updated_at": now})
            return True
        return disconnect(self.client.transaction(max_attempts=5))

    def create_owner_account(self, row):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM account_users LIMIT 1").fetchone():
                    raise HTTPException(409, "The owner account has already been created")
                db.execute("INSERT INTO account_users (id,username,username_norm,password_hash,role,created_at,last_login_at,disabled_at,workspace_id) VALUES (?,?,?,?, 'owner',?,NULL,NULL,?)",
                           (row["id"], row["username"], row["username_norm"], row["password_hash"], row["created_at"],
                            row.get("workspace_id", DEFAULT_WORKSPACE_ID)))
                db.execute("INSERT OR IGNORE INTO workspaces (id,name,created_at) VALUES (?,?,?)",
                           (row.get("workspace_id", DEFAULT_WORKSPACE_ID), "Workspace principal", row["created_at"]))
            return row
        owner_ref = self.client.collection("meta").document("account_owner")
        username_ref = self.client.collection("account_usernames").document(row["username_norm"])
        user_ref = self.client.collection("account_users").document(row["id"])
        @self.firestore.transactional
        def create(transaction):
            if owner_ref.get(transaction=transaction).exists:
                raise HTTPException(409, "The owner account has already been created")
            if username_ref.get(transaction=transaction).exists:
                raise HTTPException(409, "Username is already taken")
            transaction.create(user_ref, row | {"role": "owner"})
            transaction.create(username_ref, {"user_id": row["id"]})
            transaction.create(owner_ref, {"user_id": row["id"], "created_at": row["created_at"]})
        create(self.client.transaction(max_attempts=5))
        return row

    def account_by_username(self, username_norm):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT * FROM account_users WHERE username_norm = ? AND disabled_at IS NULL",
                                 (username_norm,)).fetchone()
                return dict(row) if row else None
        username_ref = self.client.collection("account_usernames").document(username_norm).get(timeout=15)
        if not username_ref.exists:
            return None
        return self.account_by_id(username_ref.to_dict()["user_id"])

    def account_by_id(self, user_id):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT id,username,role,created_at,last_login_at,workspace_id FROM account_users WHERE id = ? AND disabled_at IS NULL",
                                 (user_id,)).fetchone()
                return dict(row) if row else None
        row = self.client.collection("account_users").document(user_id).get(timeout=15)
        if not row.exists or row.to_dict().get("disabled_at") is not None:
            return None
        data = row.to_dict()
        data["workspace_id"] = data.get("workspace_id") or DEFAULT_WORKSPACE_ID
        return data

    def update_account_login(self, user_id, seen):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("UPDATE account_users SET last_login_at = ? WHERE id = ?", (seen, user_id))
        else:
            self.client.collection("account_users").document(user_id).update({"last_login_at": seen}, timeout=15)

    def update_account_password(self, user_id, encoded_password, now):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute("UPDATE account_users SET password_hash = ? WHERE id = ? AND disabled_at IS NULL",
                                    (encoded_password, user_id))
                if not cursor.rowcount:
                    return False
                db.execute("UPDATE account_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                           (now, user_id))
            return True
        user_ref = self.client.collection("account_users").document(user_id)
        user = user_ref.get(timeout=15)
        if not user.exists or user.to_dict().get("disabled_at") is not None:
            return False
        user_ref.update({"password_hash": encoded_password}, timeout=15)
        sessions = list(self.where(self.client.collection("account_sessions"), "user_id", "==", user_id).stream(timeout=15))
        batch = self.client.batch()
        queued = 0
        for doc in sessions:
            if doc.to_dict().get("revoked_at") is None:
                batch.update(doc.reference, {"revoked_at": now})
                queued += 1
                if queued == 450:
                    batch.commit(timeout=15)
                    batch = self.client.batch()
                    queued = 0
        if queued:
            batch.commit(timeout=15)
        return True

    def create_account_session(self, row):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("INSERT INTO account_sessions (id,user_id,token_hash,csrf_hash,created_at,expires_at,last_seen,revoked_at) VALUES (?,?,?,?,?,?,?,NULL)",
                           tuple(row[key] for key in ("id", "user_id", "token_hash", "csrf_hash", "created_at", "expires_at", "last_seen")))
        else:
            self.client.collection("account_sessions").document(row["id"]).create(row, timeout=15)
        return row

    def account_registration_key(self, user_id):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT user_id,workspace_id,key_prefix,created_at,rotated_at,revoked_at FROM account_registration_keys WHERE user_id = ?", (user_id,)).fetchone()
                return dict(row) if row else None
        ref = self.client.collection("account_registration_keys").document(user_id)
        snapshot = ref.get(timeout=15)
        return snapshot.to_dict() if snapshot.exists else None

    def create_or_rotate_account_registration_key(self, user_id, workspace_id, key_hash, key_prefix, now):
        row = {"user_id": user_id, "workspace_id": workspace_id, "key_hash": key_hash,
               "key_prefix": key_prefix, "created_at": now, "rotated_at": None, "revoked_at": None}
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT created_at FROM account_registration_keys WHERE user_id = ?", (user_id,)).fetchone()
                if old:
                    row["created_at"] = old["created_at"]
                    row["rotated_at"] = now
                db.execute("INSERT INTO account_registration_keys (user_id,workspace_id,key_hash,key_prefix,created_at,rotated_at,revoked_at) VALUES (?,?,?,?,?,?,NULL) "
                           "ON CONFLICT(user_id) DO UPDATE SET workspace_id=excluded.workspace_id,key_hash=excluded.key_hash,key_prefix=excluded.key_prefix,created_at=excluded.created_at,rotated_at=excluded.rotated_at,revoked_at=NULL",
                           tuple(row[key] for key in ("user_id","workspace_id","key_hash","key_prefix","created_at","rotated_at")))
            return row
        ref = self.client.collection("account_registration_keys").document(user_id)
        snapshot = ref.get(timeout=15)
        if snapshot.exists:
            row["created_at"] = snapshot.to_dict().get("created_at", now)
            row["rotated_at"] = now
        ref.set(row, timeout=15)
        return row

    def revoke_account_registration_key(self, user_id, now):
        if hasattr(self, "database"):
            with self.database() as db:
                cursor = db.execute("UPDATE account_registration_keys SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now, user_id))
                return bool(cursor.rowcount)
        ref = self.client.collection("account_registration_keys").document(user_id)
        snapshot = ref.get(timeout=15)
        if not snapshot.exists or snapshot.to_dict().get("revoked_at") is not None:
            return False
        ref.update({"revoked_at": now}, timeout=15)
        return True

    def account_by_registration_key_hash(self, key_hash):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT k.user_id,k.workspace_id,k.key_hash,k.revoked_at,u.username,u.role,u.disabled_at FROM account_registration_keys k JOIN account_users u ON u.id=k.user_id WHERE k.key_hash = ? AND k.revoked_at IS NULL AND u.disabled_at IS NULL", (key_hash,)).fetchone()
                return dict(row) if row else None
        query = self.where(self.client.collection("account_registration_keys"), "key_hash", "==", key_hash).limit(1)
        docs = list(query.stream(timeout=15))
        if not docs:
            return None
        row = docs[0].to_dict()
        if row.get("revoked_at") is not None:
            return None
        account = self.account_by_id(row.get("user_id"))
        if not account:
            return None
        return row | {"username": account["username"], "role": account["role"], "disabled_at": account.get("disabled_at")}

    def account_session_by_hash(self, token_hash, now):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT s.*,u.username,u.role,u.workspace_id FROM account_sessions s JOIN account_users u ON u.id=s.user_id WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND u.disabled_at IS NULL",
                                 (token_hash, now)).fetchone()
                if not row:
                    return None
                db.execute("UPDATE account_sessions SET last_seen = ? WHERE id = ?", (now, row["id"]))
                return dict(row)
        query = self.where(self.client.collection("account_sessions"), "token_hash", "==", token_hash).limit(1)
        rows = list(query.stream(timeout=15))
        if not rows:
            return None
        row = rows[0].to_dict()
        if row.get("revoked_at") is not None or row.get("expires_at", 0) <= now:
            return None
        user = self.account_by_id(row["user_id"])
        if not user:
            return None
        rows[0].reference.update({"last_seen": now}, timeout=15)
        return row | {"username": user["username"], "role": user["role"],
                      "workspace_id": user.get("workspace_id") or DEFAULT_WORKSPACE_ID}

    def create_team_account(self, row):
        if hasattr(self, "database"):
            with self.database() as db:
                try:
                    db.execute("INSERT INTO account_users (id,username,username_norm,password_hash,role,created_at,last_login_at,disabled_at,workspace_id) VALUES (?,?,?,?,?,?,NULL,NULL,?)",
                               (row["id"], row["username"], row["username_norm"], row["password_hash"], row["role"], row["created_at"],
                                row.get("workspace_id", DEFAULT_WORKSPACE_ID)))
                except sqlite3.IntegrityError:
                    raise HTTPException(409, "Username is already taken")
            return row
        user_ref = self.client.collection("account_users").document(row["id"])
        username_ref = self.client.collection("account_usernames").document(row["username_norm"])
        @self.firestore.transactional
        def create(transaction):
            if username_ref.get(transaction=transaction).exists:
                raise HTTPException(409, "Username is already taken")
            transaction.create(user_ref, row | {"last_login_at": None, "disabled_at": None})
            transaction.create(username_ref, {"user_id": row["id"]})
        create(self.client.transaction(max_attempts=5))
        return row

    def all_account_users(self, workspace_id=None):
        if hasattr(self, "database"):
            with self.database() as db:
                if workspace_id is None:
                    rows = db.execute("SELECT id,username,role,created_at,last_login_at,disabled_at,workspace_id FROM account_users ORDER BY created_at").fetchall()
                else:
                    rows = db.execute("SELECT id,username,role,created_at,last_login_at,disabled_at,workspace_id FROM account_users WHERE workspace_id=? ORDER BY created_at", (workspace_id,)).fetchall()
                return [dict(row) for row in rows]
        query = self.client.collection("account_users")
        if workspace_id is not None:
            query = self.where(query, "workspace_id", "==", workspace_id)
        rows = [doc.to_dict() for doc in query.stream(timeout=15)]
        return sorted(({key: row.get(key) for key in ("id", "username", "role", "created_at", "last_login_at", "disabled_at", "workspace_id")}
                       for row in rows), key=lambda row: row["created_at"])

    def disable_team_account(self, user_id, disabled_at):
        if hasattr(self, "database"):
            with self.database() as db:
                cursor = db.execute("UPDATE account_users SET disabled_at = ? WHERE id = ? AND role != 'owner' AND disabled_at IS NULL",
                                    (disabled_at, user_id))
                return bool(cursor.rowcount)
        ref = self.client.collection("account_users").document(user_id)
        snapshot = ref.get(timeout=15)
        if not snapshot.exists:
            return False
        user = snapshot.to_dict()
        if user.get("role") == "owner" or user.get("disabled_at") is not None:
            return False
        ref.update({"disabled_at": disabled_at}, timeout=15)
        return True

    def revoke_account_session(self, session_id, revoked_at):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("UPDATE account_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                           (revoked_at, session_id))
        else:
            self.client.collection("account_sessions").document(session_id).update({"revoked_at": revoked_at}, timeout=15)

    def account_login_allowed(self, key_hash, now):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT locked_until FROM account_login_limits WHERE key_hash = ?", (key_hash,)).fetchone()
                return not row or row["locked_until"] <= now
        row = self.client.collection("account_login_limits").document(key_hash).get(timeout=15)
        return not row.exists or row.to_dict().get("locked_until", 0) <= now

    def record_account_login(self, key_hash, now, success):
        window_seconds, max_failures, lock_seconds = 900, 6, 900
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT * FROM account_login_limits WHERE key_hash = ?", (key_hash,)).fetchone()
                if success:
                    db.execute("DELETE FROM account_login_limits WHERE key_hash = ?", (key_hash,))
                    return
                if not row or now - row["window_started"] > window_seconds:
                    db.execute("INSERT OR REPLACE INTO account_login_limits VALUES (?,?,1,0)", (key_hash, now))
                    return
                failures = row["failures"] + 1
                locked = now + lock_seconds if failures >= max_failures else row["locked_until"]
                db.execute("UPDATE account_login_limits SET failures = ?, locked_until = ? WHERE key_hash = ?",
                           (failures, locked, key_hash))
            return
        ref = self.client.collection("account_login_limits").document(key_hash)
        @self.firestore.transactional
        def update(transaction):
            snapshot = ref.get(transaction=transaction)
            row = snapshot.to_dict() if snapshot.exists else None
            if success:
                if row:
                    transaction.delete(ref)
                return
            if not row or now - row.get("window_started", 0) > window_seconds:
                transaction.set(ref, {"window_started": now, "failures": 1, "locked_until": 0})
                return
            failures = row.get("failures", 0) + 1
            transaction.update(ref, {"failures": failures,
                                     "locked_until": now + lock_seconds if failures >= max_failures else row.get("locked_until", 0)})
        update(self.client.transaction(max_attempts=5))

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

    def create_profile(self, identifier, name, kind, project, description="", capabilities=None):
        token = secrets.token_urlsafe(32)
        profile = dict(id=str(uuid4()), name=name, kind=kind, project=project, description=description,
                       capabilities=capabilities or [], created_at=time.time(),
                       enrollment_hash=hashlib.sha256(token.encode()).hexdigest())
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                agent = db.execute("SELECT * FROM agents WHERE id = ?", (identifier,)).fetchone()
                if not agent or agent["profile_id"]:
                    raise HTTPException(409, "Instance is already grouped or no longer exists")
                description = description or agent["description"]
                capabilities = capabilities or json.loads(agent["capabilities_json"] or "[]")
                profile.update(description=description, capabilities=capabilities,
                               workspace_id=agent["workspace_id"] or DEFAULT_WORKSPACE_ID)
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
                profile.update(description=description or agent.get("description", ""),
                               capabilities=capabilities or agent.get("capabilities", []),
                               workspace_id=agent.get("workspace_id") or DEFAULT_WORKSPACE_ID)
                transaction.create(self.client.collection("profiles").document(profile["id"]), profile)
                transaction.update(ref, {"profile_id": profile["id"], "instance_name": agent["name"]})
            create(self.client.transaction(max_attempts=5))
        return profile, token

    def update_profile_card(self, profile_id, fields):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT data_json FROM profiles WHERE id = ?", (profile_id,)).fetchone()
                if not row:
                    raise HTTPException(404, "Profile not found")
                profile = json.loads(row[0])
                profile.update(fields)
                db.execute("UPDATE profiles SET data_json = ? WHERE id = ?",
                           (json.dumps(profile, ensure_ascii=False), profile_id))
                return profile
        ref = self.client.collection("profiles").document(profile_id)
        snapshot = ref.get(timeout=15)
        if not snapshot.exists:
            raise HTTPException(404, "Profile not found")
        ref.update(fields, timeout=15)
        return {**snapshot.to_dict(), **fields}

    def create_control_user(self, row):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("INSERT INTO control_users (id,name,role,token_hash,created_by,created_at,last_seen,revoked_at) "
                           "VALUES (?,?,?,?,?,?,NULL,NULL)",
                           (row["id"], row["name"], row["role"], row["token_hash"], row["created_by"], row["created_at"]))
                db.execute("INSERT INTO control_audit (actor,action,target_id,created_at) VALUES (?,?,?,?)",
                           (row["created_by"], "user_key_created", row["id"], row["created_at"]))
            return row
        batch = self.client.batch()
        batch.create(self.client.collection("control_users").document(row["id"]), row)
        batch.create(self.client.collection("control_audit").document(str(uuid4())),
                     {"actor": row["created_by"], "action": "user_key_created", "target_id": row["id"], "created_at": row["created_at"]})
        batch.commit(timeout=15)
        return row

    def control_user_by_hash(self, digest):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT * FROM control_users WHERE token_hash = ? AND revoked_at IS NULL", (digest,)).fetchone()
                if row:
                    seen = time.time()
                    db.execute("UPDATE control_users SET last_seen = ? WHERE id = ?", (seen, row["id"]))
                    return dict(row) | {"last_seen": seen}
            return None
        query = self.where(self.client.collection("control_users"), "token_hash", "==", digest)
        documents = list(query.limit(1).stream(timeout=15))
        if not documents:
            return None
        row = documents[0].to_dict()
        if row.get("revoked_at") is not None:
            return None
        seen = time.time()
        documents[0].reference.update({"last_seen": seen}, timeout=15)
        return row | {"last_seen": seen}

    def create_control_session(self, row):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("""INSERT INTO control_sessions
                    (id,control_user_id,identity_type,username,role,credential_hash,token_hash,csrf_hash,
                     created_at,expires_at,last_seen,revoked_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                    tuple(row.get(key) for key in ("id", "control_user_id", "identity_type", "username", "role",
                                                   "credential_hash", "token_hash", "csrf_hash", "created_at",
                                                   "expires_at", "last_seen")))
            return row
        self.client.collection("control_sessions").document(row["id"]).create(row, timeout=15)
        return row

    def control_session_by_hash(self, token_hash, now):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT * FROM control_sessions WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?",
                                 (token_hash, now)).fetchone()
                if not row:
                    return None
                result = dict(row)
                if row["identity_type"] == "control_user":
                    user = db.execute("SELECT name,role,revoked_at FROM control_users WHERE id = ?",
                                      (row["control_user_id"],)).fetchone()
                    if not user or user["revoked_at"] is not None:
                        return None
                    result.update(username=user["name"], role=user["role"])
                    db.execute("UPDATE control_users SET last_seen = ? WHERE id = ?", (now, row["control_user_id"]))
                db.execute("UPDATE control_sessions SET last_seen = ? WHERE id = ?", (now, row["id"]))
                return result
        rows = list(self.where(self.client.collection("control_sessions"), "token_hash", "==", token_hash)
                    .limit(1).stream(timeout=15))
        if not rows:
            return None
        row = rows[0].to_dict()
        if row.get("revoked_at") is not None or row.get("expires_at", 0) <= now:
            return None
        if row.get("identity_type") == "control_user":
            user_ref = self.client.collection("control_users").document(row["control_user_id"])
            user_snapshot = user_ref.get(timeout=15)
            if not user_snapshot.exists:
                return None
            user = user_snapshot.to_dict()
            if user.get("revoked_at") is not None:
                return None
            row.update(username=user["name"], role=user["role"])
            user_ref.update({"last_seen": now}, timeout=15)
        rows[0].reference.update({"last_seen": now}, timeout=15)
        return row

    def revoke_control_session(self, session_id, revoked_at):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("UPDATE control_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                           (revoked_at, session_id))
        else:
            self.client.collection("control_sessions").document(session_id).update({"revoked_at": revoked_at}, timeout=15)

    def all_control_users(self):
        if hasattr(self, "database"):
            with self.database() as db:
                return [dict(row) for row in db.execute("SELECT * FROM control_users ORDER BY created_at DESC")]
        rows = [doc.to_dict() for doc in self.client.collection("control_users").stream(timeout=15)]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def revoke_control_user(self, identifier, actor="owner"):
        revoked_at = time.time()
        if hasattr(self, "database"):
            with self.database() as db:
                cursor = db.execute("UPDATE control_users SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                                    (revoked_at, identifier))
                if cursor.rowcount:
                    db.execute("INSERT INTO control_audit (actor,action,target_id,created_at) VALUES (?,?,?,?)",
                               (actor, "user_key_revoked", identifier, revoked_at))
                return bool(cursor.rowcount)
        ref = self.client.collection("control_users").document(identifier)
        event_ref = self.client.collection("control_audit").document(str(uuid4()))
        @self.firestore.transactional
        def revoke(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists or snapshot.to_dict().get("revoked_at") is not None:
                return False
            transaction.update(ref, {"revoked_at": revoked_at})
            transaction.create(event_ref, {"actor": actor, "action": "user_key_revoked", "target_id": identifier, "created_at": revoked_at})
            return True
        return revoke(self.client.transaction(max_attempts=5))

    def record_control_event(self, actor, action, target_id):
        row = {"actor": actor, "action": action, "target_id": target_id, "created_at": time.time()}
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("INSERT INTO control_audit (actor,action,target_id,created_at) VALUES (?,?,?,?)",
                           (actor, action, target_id, row["created_at"]))
        else:
            from uuid import uuid4
            self.client.collection("control_audit").document(str(uuid4())).create(row, timeout=15)

    def control_audit(self, limit=100):
        if hasattr(self, "database"):
            with self.database() as db:
                return [dict(row) for row in db.execute(
                    "SELECT * FROM control_audit ORDER BY created_at DESC LIMIT ?", (limit,))]
        query = self.client.collection("control_audit").order_by("created_at", direction="DESCENDING").limit(limit)
        return [doc.to_dict() | {"id": doc.id} for doc in query.stream(timeout=15)]

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
        row = {**row, "profile_id": profile_id, "instance_name": row["name"],
               "description": row.get("description", ""), "capabilities": row.get("capabilities", [])}
        def verify(existing, workspace_id):
            if existing and (existing.get("profile_id") != profile_id or existing["token_hash"] != row["token_hash"] or
                             (existing.get("workspace_id") or DEFAULT_WORKSPACE_ID) != workspace_id):
                raise HTTPException(409, "Native session identity conflict")
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                profile_row = db.execute("SELECT data_json FROM profiles WHERE id = ?", (profile_id,)).fetchone()
                if not profile_row:
                    raise HTTPException(404, "Profile no longer exists")
                workspace_id = json.loads(profile_row["data_json"]).get("workspace_id") or DEFAULT_WORKSPACE_ID
                row["workspace_id"] = workspace_id
                old = db.execute("SELECT * FROM agents WHERE id = ?", (row["id"],)).fetchone()
                verify(dict(old) if old else None, workspace_id)
                if old:
                    return False
                columns = ("id", "name", "kind", "project", "token_hash", "created_at", "last_seen", "profile_id", "instance_name", "description", "capabilities_json", "workspace_id", "recovery_hash")
                db.execute(f"INSERT INTO agents ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                           tuple(json.dumps(row["capabilities"], ensure_ascii=False) if key == "capabilities_json" else row.get(key)
                                 for key in columns))
            return True
        @self.firestore.transactional
        def enroll(transaction):
            profile_ref = self.client.collection("profiles").document(profile_id)
            profile = profile_ref.get(transaction=transaction).to_dict()
            instance_ref = self.agents.document(row["id"])
            old = instance_ref.get(transaction=transaction).to_dict()
            if not profile:
                raise HTTPException(404, "Profile no longer exists")
            workspace_id = profile.get("workspace_id") or DEFAULT_WORKSPACE_ID
            verify(old, workspace_id)
            if old:
                return False
            transaction.create(instance_ref, {**row, "workspace_id": workspace_id})
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

    def cancel_outgoing_request(self, identifier, message_id, note="Cancelled by requester"):
        """Cancel only a request created by this sender; a running host turn may still need to stop itself."""
        fields = {"status": "cancelled", "status_note": note, "updated_at": time.time()}
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
                if (not row or row["from_agent_id"] != identifier or row["in_reply_to"] is not None or
                        row["message_type"] != "request"):
                    raise HTTPException(404, "Task not found")
                if row["status"] == "cancelled":
                    return self.message_output(row)
                if row["status"] == "completed":
                    raise HTTPException(409, "Completed task cannot be cancelled")
                db.execute("UPDATE messages SET status = ?, status_note = ?, updated_at = ? WHERE id = ?",
                           (fields["status"], fields["status_note"], fields["updated_at"], message_id))
                return self.message_output(db.execute("SELECT * FROM messages WHERE id = ?",
                                                       (message_id,)).fetchone())

        @self.firestore.transactional
        def change(transaction):
            ref = self.message_reference(message_id)
            snapshot = ref.get(transaction=transaction)
            row = snapshot.to_dict() if snapshot.exists else None
            if (not row or row.get("from_agent_id") != identifier or row.get("in_reply_to") is not None or
                    row.get("message_type", "request") != "request"):
                raise HTTPException(404, "Task not found")
            if row.get("status", "queued") == "cancelled":
                return message_view(row)
            if row.get("status", "queued") == "completed":
                raise HTTPException(409, "Completed task cannot be cancelled")
            transaction.update(ref, fields)
            return message_view(row | fields)
        return change(self.client.transaction(max_attempts=5))

    def count_outbox_requests(self, identifier, status=None, thread_id=None):
        if hasattr(self, "database"):
            clauses, values = ["from_agent_id = ?", "in_reply_to IS NULL", "message_type = 'request'"], [identifier]
            if status:
                clauses.append("status = ?")
                values.append(status)
            if thread_id:
                clauses.append("thread_id = ?")
                values.append(thread_id)
            with self.database() as db:
                return db.execute("SELECT COUNT(*) FROM messages WHERE " + " AND ".join(clauses), values).fetchone()[0]
        rows = self.where(self.messages, "from_agent_id", "==", identifier).stream(timeout=30)
        count = 0
        for doc in rows:
            row = doc.to_dict()
            if row.get("in_reply_to") is not None or row.get("message_type", "request") != "request":
                continue
            if status and row.get("status", "queued") != status:
                continue
            if thread_id and row.get("thread_id") != thread_id:
                continue
            count += 1
        return count

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
