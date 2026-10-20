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
                "agents": {"profile_id": "TEXT", "instance_name": "TEXT NOT NULL DEFAULT ''",
                           "description": "TEXT NOT NULL DEFAULT ''",
                           "capabilities_json": "TEXT NOT NULL DEFAULT '[]'"},
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
                    created_at REAL NOT NULL, last_login_at REAL, disabled_at REAL
                );
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

    def create_owner_account(self, row):
        if hasattr(self, "database"):
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM account_users LIMIT 1").fetchone():
                    raise HTTPException(409, "The owner account has already been created")
                db.execute("INSERT INTO account_users (id,username,username_norm,password_hash,role,created_at,last_login_at,disabled_at) VALUES (?,?,?,?, 'owner',?,NULL,NULL)",
                           (row["id"], row["username"], row["username_norm"], row["password_hash"], row["created_at"]))
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
                row = db.execute("SELECT id,username,role,created_at,last_login_at FROM account_users WHERE id = ? AND disabled_at IS NULL",
                                 (user_id,)).fetchone()
                return dict(row) if row else None
        row = self.client.collection("account_users").document(user_id).get(timeout=15)
        if not row.exists or row.to_dict().get("disabled_at") is not None:
            return None
        return row.to_dict()

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

    def account_session_by_hash(self, token_hash, now):
        if hasattr(self, "database"):
            with self.database() as db:
                row = db.execute("SELECT s.*,u.username,u.role FROM account_sessions s JOIN account_users u ON u.id=s.user_id WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND u.disabled_at IS NULL",
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
        return row | {"username": user["username"], "role": user["role"]}

    def create_team_account(self, row):
        if hasattr(self, "database"):
            with self.database() as db:
                try:
                    db.execute("INSERT INTO account_users (id,username,username_norm,password_hash,role,created_at,last_login_at,disabled_at) VALUES (?,?,?,?,?,?,NULL,NULL)",
                               (row["id"], row["username"], row["username_norm"], row["password_hash"], row["role"], row["created_at"]))
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

    def all_account_users(self):
        if hasattr(self, "database"):
            with self.database() as db:
                rows = db.execute("SELECT id,username,role,created_at,last_login_at,disabled_at FROM account_users ORDER BY created_at").fetchall()
                return [dict(row) for row in rows]
        rows = [doc.to_dict() for doc in self.client.collection("account_users").stream(timeout=15)]
        return sorted(({key: row.get(key) for key in ("id", "username", "role", "created_at", "last_login_at", "disabled_at")}
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
                profile.update(description=description, capabilities=capabilities)
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
                               capabilities=capabilities or agent.get("capabilities", []))
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
                columns = ("id", "name", "kind", "project", "token_hash", "created_at", "last_seen", "profile_id", "instance_name", "description", "capabilities_json")
                db.execute(f"INSERT INTO agents ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                           tuple(json.dumps(row["capabilities"], ensure_ascii=False) if key == "capabilities_json" else row[key]
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
