"""A shared, persistent mailbox. Agents poll it using ordinary HTTP requests."""

import hashlib
import base64
import binascii
import os
import re
import secrets
import time
from contextvars import Token
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from stores import FirestoreStore, SQLiteStore

MAX_MESSAGE_ID = 2**63 - 1
APP_VERSION = FilePath(__file__).with_name("VERSION").read_text(encoding="ascii").strip()


class AgentRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="other", min_length=1, max_length=60)
    project: str = Field(default="", max_length=300)


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(min_length=1, max_length=120)
    data_base64: str = Field(min_length=1, max_length=14_000_000)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_agent_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(default="", max_length=32768)
    in_reply_to: int | None = Field(default=None, gt=0, le=MAX_MESSAGE_ID)
    attachments: list[Attachment] = Field(default_factory=list, max_length=3)


class Broadcast(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=32768)
    online_only: bool = False
    kinds: list[str] = Field(default_factory=list, max_length=20)


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
BLOCKED_ATTACHMENT_SUFFIXES = {".ade", ".apk", ".app", ".bat", ".cmd", ".com", ".deb",
                               ".dll", ".dmg", ".exe", ".hta", ".img", ".iso", ".jar", ".msi",
                               ".ps1", ".rpm", ".scr", ".sh", ".sys", ".vbe", ".vbs"}
MAX_BROADCAST_RECIPIENTS = 50


def prepare_attachments(items):
    prepared, total = [], 0
    signatures = {
        "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
        "image/gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }
    for item in items:
        try:
            data = base64.b64decode(item.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, "Attachment is not valid base64")
        media_type = item.media_type.lower().split(";", 1)[0].strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", media_type):
            raise HTTPException(422, "Attachment media_type must be a valid MIME type")
        if not data:
            raise HTTPException(422, "Attachment cannot be empty")
        signature = signatures.get(media_type)
        if signature and not signature(data):
            raise HTTPException(422, "Attachment bytes do not match their declared media type")
        filename = item.filename.replace("\\", "/").split("/")[-1].strip() or "file"
        if FilePath(filename).suffix.lower() in BLOCKED_ATTACHMENT_SUFFIXES:
            raise HTTPException(415, "Executable and installer attachments are not supported")
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, "Attachments exceed the 10 MiB per-message limit")
        prepared.append({"filename": filename[:180], "media_type": media_type,
                         "size_bytes": len(data), "data": data})
    return prepared


def create_app():
    hub_key = os.environ.get("MAILBOX_ADMIN_KEY", "")
    join_key = os.environ.get("MAILBOX_JOIN_KEY", "")
    online_seconds = int(os.environ.get("MAILBOX_ONLINE_SECONDS", "90"))
    if len(hub_key) < 32:
        raise RuntimeError("Set MAILBOX_ADMIN_KEY to a random secret of at least 32 characters.")
    if join_key and (len(join_key) < 16 or secrets.compare_digest(join_key.encode(), hub_key.encode())):
        raise RuntimeError("MAILBOX_JOIN_KEY must have at least 16 characters and differ from the admin key.")
    if online_seconds < 1:
        raise RuntimeError("MAILBOX_ONLINE_SECONDS must be positive.")
    backend = os.environ.get("MAILBOX_STORAGE", "sqlite")
    if backend == "firestore":
        store = FirestoreStore(os.environ.get("GOOGLE_CLOUD_PROJECT"),
                               os.environ.get("MAILBOX_FIRESTORE_DATABASE", "agent-mailbox"))
    elif backend == "sqlite":
        store = SQLiteStore(os.environ.get("MAILBOX_DB", "data/mailbox.sqlite3"))
    else:
        raise RuntimeError("MAILBOX_STORAGE must be sqlite or firestore")

    from mailbox_mcp import mcp as mailbox_mcp, remote_mcp_identity

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        async with mailbox_mcp.session_manager.run():
            yield

    app = FastAPI(title="Agent Mailbox", version=APP_VERSION, lifespan=lifespan,
                  description="Registro, presencia y buzones HTTP compartidos para agentes.")

    @app.middleware("http")
    async def authenticate_mcp(request, call_next):
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)
        if request.url.path == "/mcp":
            request.scope["path"] = "/mcp/"
            request.scope["raw_path"] = b"/mcp/"
        if request.method == "GET" and "text/event-stream" in request.headers.get("accept", ""):
            return PlainTextResponse("This stateless MCP endpoint does not offer an event stream.", status_code=405)
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return PlainTextResponse("Bearer agent token required", status_code=401,
                                     headers={"WWW-Authenticate": "Bearer"})
        identifier = store.authenticate(hashlib.sha256(token.encode()).hexdigest())
        if identifier is None:
            return PlainTextResponse("Invalid agent token", status_code=401,
                                     headers={"WWW-Authenticate": "Bearer"})

        def mcp_api(path, method, payload, query):
            if path == "/agents" and method == "GET":
                now = time.time()
                agents = [public_agent(row, now) for row in store.all_agents()]
                if query and query.get("online") == "true":
                    agents = [row for row in agents if row["online"]]
                return {"agents": agents[:100], "total": len(agents), "limit": 100, "offset": 0}
            if path == "/inbox" and method == "GET":
                after_id, limit = int(query.get("after_id", 0)), int(query.get("limit", 50))
                unread_only = query.get("unread_only", "true") == "true"
                rows = store.inbox(identifier, unread_only, after_id, limit + 1)
                messages = rows[:limit]
                return {"messages": messages, "has_more": len(rows) > limit,
                        "next_after_id": messages[-1]["id"] if messages else after_id}
            if path == "/messages" and method == "POST":
                message = Message(**payload)
                attachments = prepare_attachments(message.attachments)
                if not message.text.strip() and not attachments:
                    raise HTTPException(422, "Message text cannot be blank")
                return store.send(identifier, message, attachments)
            if path.startswith("/messages/") and method == "GET":
                message_id = int(path.rsplit("/", 1)[1])
                if not 1 <= message_id <= MAX_MESSAGE_ID:
                    raise HTTPException(422, "Invalid message ID")
                return store.get_message(identifier, message_id)
            if path.startswith("/messages/") and method == "DELETE":
                message_id = int(path.rsplit("/", 1)[1])
                if not 1 <= message_id <= MAX_MESSAGE_ID:
                    raise HTTPException(422, "Invalid message ID")
                return store.delete_message(identifier, message_id)
            if path == "/inbox" and method == "DELETE":
                after_id = int(query.get("after_id", 0))
                before_value = query.get("before_id")
                before_id = int(before_value) if before_value not in (None, "") else None
                limit = int(query.get("limit", 100))
                if not 0 <= after_id <= MAX_MESSAGE_ID or not 1 <= limit <= 500:
                    raise HTTPException(422, "Invalid inbox deletion cursor or limit")
                if before_id is not None and not 1 <= before_id <= MAX_MESSAGE_ID:
                    raise HTTPException(422, "Invalid before_id")
                return store.delete_inbox(identifier, after_id, before_id, limit)
            if path == "/broadcast" and method == "POST":
                return send_broadcast(identifier, Broadcast(**payload))
            attachment_prefix = "/inbox/"
            if method == "GET" and path.startswith(attachment_prefix) and "/attachments/" in path:
                message_part, index_part = path[len(attachment_prefix):].split("/attachments/", 1)
                attachment = store.get_attachment(identifier, int(message_part), int(index_part))
                return {"filename": attachment["filename"], "media_type": attachment["media_type"],
                        "size_bytes": attachment["size_bytes"],
                        "data_base64": base64.b64encode(attachment["data"]).decode("ascii")}
            if method == "POST" and path.startswith("/inbox/") and path.endswith("/ack"):
                message_id = int(path.split("/")[2])
                store.acknowledge(identifier, message_id)
                return {"id": message_id, "acknowledged": True}
            if path == "/agents/me" and method == "DELETE":
                if not store.unregister(identifier):
                    raise HTTPException(404, "Agent not found")
                return {"id": identifier, "deleted": True,
                        "messages_and_attachments_deleted": True}
            raise HTTPException(404, "Unknown MCP mailbox operation")

        context_token: Token = remote_mcp_identity.set({"id": identifier, "call": mcp_api})
        try:
            return await call_next(request)
        finally:
            remote_mcp_identity.reset(context_token)

    app.mount("/mcp", mailbox_mcp.streamable_http_app())
    bearer = HTTPBearer(auto_error=False)
    Credential = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]

    def authenticate(credentials: Credential):
        if credentials is None:
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
        token = credentials.credentials
        if secrets.compare_digest(token.encode(), hub_key.encode()):
            return {"admin": True}
        identifier = store.authenticate(hashlib.sha256(token.encode()).hexdigest())
        if identifier is None:
            raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
        return {"admin": False, "id": identifier}

    Identity = Annotated[dict, Depends(authenticate)]

    def agent_id(identity):
        if identity["admin"]:
            raise HTTPException(403, "Use an agent token for messaging and presence")
        return identity["id"]

    def public_agent(row, now):
        result = {key: row[key] for key in ("id", "name", "kind", "project", "created_at", "last_seen")}
        result["online"] = row["last_seen"] >= now - online_seconds
        return result

    def create_registration(body):
        identifier, token, now = str(uuid4()), secrets.token_urlsafe(32), time.time()
        row = dict(id=identifier, name=body.name, kind=body.kind, project=body.project,
                   token_hash=hashlib.sha256(token.encode()).hexdigest(), created_at=now, last_seen=now)
        store.register(row)
        return {"agent": public_agent(row, now), "token": token}

    def send_broadcast(sender, body):
        if not body.text.strip():
            raise HTTPException(422, "Broadcast text cannot be blank")
        wanted_kinds = {kind.strip().casefold() for kind in body.kinds if kind.strip()}
        now = time.time()
        recipients = [row for row in store.all_agents()
                      if row["id"] != sender
                      and (not body.online_only or row["last_seen"] >= now - online_seconds)
                      and (not wanted_kinds or row["kind"].strip().casefold() in wanted_kinds)]
        if len(recipients) > MAX_BROADCAST_RECIPIENTS:
            raise HTTPException(413, f"Broadcast exceeds the {MAX_BROADCAST_RECIPIENTS}-recipient limit; filter by kind or online_only")
        messages = [store.send(sender, Message(to_agent_id=row["id"], text=body.text))
                    for row in recipients]
        return {"broadcast_id": str(uuid4()), "sent_count": len(messages),
                "online_only": body.online_only, "kinds": sorted(wanted_kinds),
                "messages": messages}

    @app.get("/", include_in_schema=False)
    def welcome():
        return FileResponse(FilePath(__file__).parent / "connect.html", headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        })

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(FilePath(__file__).parent / "dashboard.html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        })

    @app.get("/dashboard.js", include_in_schema=False)
    def dashboard_script():
        return FileResponse(FilePath(__file__).parent / "dashboard.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/dashboard/data")
    def dashboard_data(identity: Identity):
        if not identity["admin"]:
            raise HTTPException(403, "El panel requiere la clave administrativa")
        now = time.time()
        agents = [public_agent(row, now) for row in store.all_agents()]
        return {"stats": {**store.stats(now - online_seconds), "online_window_seconds": online_seconds},
                "agents": agents, "messages": store.recent_messages(15), "updated_at": now}

    @app.get("/connect.js", include_in_schema=False)
    def connection_script():
        return FileResponse(FilePath(__file__).parent / "connect.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/connect.md", response_class=PlainTextResponse)
    def connection_instructions():
        return PlainTextResponse((FilePath(__file__).parent / "AGENT_PROTOCOL.md").read_text(encoding="utf-8"),
                                 headers={"Cache-Control": "no-store"})

    @app.get("/mailbox-watch.py", include_in_schema=False)
    def mailbox_watcher():
        return FileResponse(FilePath(__file__).parent / "mailbox_watch.py",
                            media_type="text/x-python", headers={"Cache-Control": "no-store",
                                                                  "X-Content-Type-Options": "nosniff"})

    @app.get("/mailbox-mcp.py", include_in_schema=False)
    def mailbox_mcp_server():
        return FileResponse(FilePath(__file__).parent / "mailbox_mcp.py",
                            media_type="text/x-python", headers={"Cache-Control": "no-store",
                                                                  "X-Content-Type-Options": "nosniff"})

    @app.post("/join", status_code=201)
    def join(body: AgentRegistration, credentials: Credential):
        if not join_key:
            raise HTTPException(503, "Self-registration is not configured")
        if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), join_key.encode()):
            raise HTTPException(401, "Clave de acceso incorrecta o ausente", headers={"WWW-Authenticate": "Bearer"})
        result = create_registration(body)
        # The shared key is only checked here; it never authenticates mailbox access.
        from fastapi.responses import JSONResponse
        return JSONResponse({**result, "instructions_url": "/connect.md"}, status_code=201,
                            headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        store.ping()
        return {"ok": True, "storage": backend, "version": app.version}

    @app.post("/agents", status_code=201)
    def register(body: AgentRegistration, identity: Identity):
        if not identity["admin"]:
            raise HTTPException(403, "Registration requires the admin key")
        return create_registration(body)

    @app.get("/stats")
    def stats(identity: Identity):
        return {**store.stats(time.time() - online_seconds), "online_window_seconds": online_seconds}

    @app.get("/agents")
    def list_agents(identity: Identity, online: bool | None = None,
                    limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        now = time.time()
        agents = [public_agent(row, now) for row in store.all_agents()]
        if online is not None:
            agents = [row for row in agents if row["online"] == online]
        return {"agents": agents[offset:offset + limit], "total": len(agents), "limit": limit, "offset": offset}

    @app.get("/agents/me")
    def me(identity: Identity):
        return public_agent(store.get_agent(agent_id(identity)), time.time())

    @app.delete("/agents/me")
    def unregister_me(identity: Identity):
        identifier = agent_id(identity)
        if not store.unregister(identifier):
            raise HTTPException(404, "Agent not found")
        return {"id": identifier, "deleted": True,
                "messages_and_attachments_deleted": True}

    @app.delete("/agents/{identifier}")
    def unregister_agent(identifier: Annotated[str, Path(min_length=1, max_length=100,
                                                          pattern=r"^[A-Za-z0-9_-]+$")],
                          identity: Identity):
        if not identity["admin"]:
            raise HTTPException(403, "Only the administrator can remove another agent")
        if not store.unregister(identifier):
            raise HTTPException(404, "Agent not found")
        return {"id": identifier, "deleted": True,
                "messages_and_attachments_deleted": True}

    @app.post("/heartbeat")
    def heartbeat(identity: Identity):
        return {"agent_id": agent_id(identity), "online": True, "online_window_seconds": online_seconds}

    @app.post("/messages", status_code=201)
    def send_message(body: Message, identity: Identity):
        sender = agent_id(identity)
        attachments = prepare_attachments(body.attachments)
        if not body.text.strip() and not attachments:
            raise HTTPException(422, "Message text cannot be blank")
        return store.send(sender, body, attachments)

    @app.get("/messages/{message_id}")
    def get_message(message_id: Annotated[int, Path(gt=0, le=MAX_MESSAGE_ID)], identity: Identity):
        return store.get_message(agent_id(identity), message_id)

    @app.delete("/messages/{message_id}")
    def delete_message(message_id: Annotated[int, Path(gt=0, le=MAX_MESSAGE_ID)], identity: Identity):
        return store.delete_message(identity.get("id", ""), message_id, admin=identity["admin"])

    @app.post("/broadcast", status_code=201)
    def broadcast(body: Broadcast, identity: Identity):
        return send_broadcast(agent_id(identity), body)

    @app.get("/inbox/{message_id}/attachments/{attachment_index}")
    def get_inbox_attachment(message_id: Annotated[int, Path(gt=0, le=MAX_MESSAGE_ID)],
                             attachment_index: Annotated[int, Path(ge=0)], identity: Identity):
        attachment = store.get_attachment(agent_id(identity), message_id, attachment_index)
        return JSONResponse({"filename": attachment["filename"], "media_type": attachment["media_type"],
                             "size_bytes": attachment["size_bytes"],
                             "data_base64": base64.b64encode(attachment["data"]).decode("ascii")},
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/inbox/{message_id}/attachments/{attachment_index}/download")
    def download_inbox_attachment(message_id: Annotated[int, Path(gt=0, le=MAX_MESSAGE_ID)],
                                  attachment_index: Annotated[int, Path(ge=0)], identity: Identity):
        from urllib.parse import quote
        attachment = store.get_attachment(agent_id(identity), message_id, attachment_index)
        filename = attachment["filename"].replace("\\", "_").replace('"', "_")
        return Response(content=attachment["data"], media_type=attachment["media_type"],
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"})

    @app.get("/inbox/count")
    def inbox_count(identity: Identity):
        return store.inbox_count(agent_id(identity))

    @app.get("/inbox")
    def inbox(identity: Identity, unread_only: bool = True,
              after_id: int = Query(0, ge=0, le=MAX_MESSAGE_ID), limit: int = Query(50, ge=1, le=200)):
        rows = store.inbox(agent_id(identity), unread_only, after_id, limit + 1)
        messages = rows[:limit]
        return {"messages": messages, "has_more": len(rows) > limit,
                "next_after_id": messages[-1]["id"] if messages else after_id}

    @app.delete("/inbox")
    def delete_inbox(identity: Identity, after_id: int = Query(0, ge=0, le=MAX_MESSAGE_ID),
                     before_id: int | None = Query(None, gt=0, le=MAX_MESSAGE_ID),
                     limit: int = Query(100, ge=1, le=500)):
        return store.delete_inbox(agent_id(identity), after_id, before_id, limit)

    @app.post("/inbox/{message_id}/ack")
    def acknowledge(message_id: Annotated[int, Path(gt=0, le=MAX_MESSAGE_ID)], identity: Identity):
        store.acknowledge(agent_id(identity), message_id)
        return {"id": message_id, "acknowledged": True}

    return app

