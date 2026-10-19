"""A shared, persistent mailbox. Agents poll it using ordinary HTTP requests."""

import hashlib
import base64
import binascii
import os
import re
import hmac
import threading
import secrets
import time
import unicodedata
from contextvars import Token
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Literal
from uuid import uuid4, uuid5, UUID

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse, Response, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, AliasChoices, ValidationError, field_validator

from stores import FirestoreStore, SQLiteStore

MAX_MESSAGE_ID = 2**63 - 1
APP_VERSION = FilePath(__file__).with_name("VERSION").read_text(encoding="ascii").strip()


class AgentRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="other", min_length=1, max_length=60)
    project: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, values):
        result, seen = [], set()
        for value in values:
            label = value.strip()
            if not 1 <= len(label) <= 64:
                raise ValueError("Each capability must contain 1–64 characters")
            if label.casefold() not in seen:
                result.append(label)
                seen.add(label.casefold())
        return result


class AgentCardPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    description: str | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = Field(default=None, max_length=20)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, values):
        return AgentRegistration.unique_capabilities(values) if values is not None else values


class ProfileCardPatch(AgentCardPatch):
    pass


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(min_length=1, max_length=120)
    data_base64: str = Field(min_length=1, max_length=14_000_000)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_agent_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$",
                             validation_alias=AliasChoices("to_instance_id", "to_agent_id"))
    text: str = Field(default="", max_length=32768)
    subject: str = Field(default="", max_length=180)
    share_subject: bool = False
    message_type: Literal["request", "note"] = "request"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)
    in_reply_to: int | None = Field(default=None, gt=0, le=MAX_MESSAGE_ID)
    attachments: list[Attachment] = Field(default_factory=list, max_length=3)


class Broadcast(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=32768)
    online_only: bool = False
    kinds: list[str] = Field(default_factory=list, max_length=20)


class InstanceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    instance_name: str = Field(min_length=1, max_length=100)
    project: str = Field(default="", max_length=300)
    session_key: str | None = Field(default=None, min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, values):
        return AgentRegistration.unique_capabilities(values)


class RequestStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["queued", "in_progress", "completed", "blocked", "cancelled"]
    note: str = Field(default="", max_length=2000)


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default="", max_length=32768)
    attachments: list[Attachment] = Field(default_factory=list, max_length=3)
    status: Literal["completed", "blocked", "in_progress"] = "completed"
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)


class MailQuery(BaseModel):
    after_id: int = Field(default=0, ge=0, le=MAX_MESSAGE_ID)
    limit: int = Field(default=50, ge=1, le=200)
    unread_only: bool = False
    status: Literal["queued", "in_progress", "completed", "blocked", "cancelled"] | None = None
    thread_id: int | None = Field(default=None, gt=0, le=MAX_MESSAGE_ID)
    has_attachments: bool | None = None


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
        filename = re.sub(r"[\x00-\x1f\x7f]", "_", item.filename.replace("\\", "/").split("/")[-1]).strip() or "file"
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
    wait_slots = threading.BoundedSemaphore(12)

    def wait_mail(identifier, after_id, timeout):
        if not 0 <= after_id <= MAX_MESSAGE_ID or not 1 <= timeout <= 25:
            raise HTTPException(422, "Invalid event cursor or timeout")
        if not wait_slots.acquire(blocking=False):
            raise HTTPException(429, "Event slots are busy; retry or use inbox polling", headers={"Retry-After": "5"})
        try:
            return store.wait_for_mail(identifier, after_id, timeout)
        finally:
            wait_slots.release()

    def public_file(filename, media_type):
        def serve():
            return FileResponse(FilePath(__file__).parent / filename, media_type=media_type,
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                         "Referrer-Policy": "no-referrer",
                                         "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'"})
        return serve

    for url, filename, mime in [
        ("/app.css", "app.css", "text/css"), ("/workspace.js", "workspace.js", "application/javascript"),
        ("/workspace", "workspace.html", "text/html"), ("/guide.js", "guide.js", "application/javascript"),
        ("/favicon.svg", "favicon.svg", "image/svg+xml"),
        ("/use-cases", "use-cases.html", "text/html"), ("/use-cases.js", "use-cases.js", "application/javascript"),
        ("/mailbox-client.py", "mailbox_client.py", "text/x-python"),
        ("/agent-instructions.md", "docs/AGENT-INSTRUCTIONS.md", "text/plain"),
    ]:
        app.add_api_route(url, public_file(filename, mime), methods=["GET"], include_in_schema=False)

    @app.get("/guide", include_in_schema=False)
    def guides_home():
        return RedirectResponse("/guide/operations")

    @app.get("/guide/{slug}", response_class=HTMLResponse, include_in_schema=False)
    def guide_page(slug: str):
        from web_pages import render_guide
        return HTMLResponse(render_guide(slug), headers={"Cache-Control": "no-store",
                            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'",
                            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

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
            query = query or {}
            if path == "/inbox/wait" and method == "GET":
                return wait_mail(identifier, int(query.get("after_id", 0)), int(query.get("timeout", 25)))
            if path == "/workflow" and method == "GET":
                return workflow_bundle()
            if path == "/inbox/summary" and method == "GET":
                return inbox_summary_for(identifier)
            if path == "/outbox" and method == "GET":
                return page_mail(identifier, "outbox", query)
            if path == "/profiles" and method == "GET":
                return profiles_directory()
            if path == "/profiles" and method == "POST":
                return new_profile(identifier, AgentRegistration(**payload))
            if path.startswith("/profiles/") and path.endswith("/activity"):
                return store.profile_activity(identifier, path.split("/")[2])
            if path.startswith("/profiles/") and path.endswith("/card") and method == "PATCH":
                profile_id = path.split("/")[2]
                agent = store.get_agent(identifier)
                if not agent or agent.get("profile_id") != profile_id:
                    raise HTTPException(403, "Only an instance in this profile can update its shared card")
                patch = ProfileCardPatch(**payload)
                fields = patch.model_dump(exclude_unset=True)
                fields = {key: value if value is not None else ("" if key == "description" else [])
                          for key, value in fields.items()}
                if not fields:
                    raise HTTPException(422, "Provide description and/or capabilities")
                return public_profile(store.update_profile_card(profile_id, fields))
            if path.startswith("/profiles/") and path.endswith("/instances"):
                return profile_instances(path.split("/")[2])
            if path.startswith("/threads/") and method == "GET":
                args = MailQuery(**query)
                return store.thread_messages(identifier, int(path.split("/")[2]), args.after_id, args.limit)
            if path.startswith("/messages/") and path.endswith("/reply") and method == "POST":
                return reply_to(identifier, int(path.split("/")[2]), Reply(**payload))
            if path.startswith("/messages/") and path.endswith("/status") and method == "PATCH":
                state = RequestStatus(**payload)
                return store.update_status(identifier, int(path.split("/")[2]), state.status, state.note)
            if path == "/agents" and method == "GET":
                now = time.time()
                profiles = {row["id"]: row for row in store.profiles_all()}
                agents = [public_agent(row, now, profiles.get(row.get("profile_id"))) for row in store.all_agents()]
                if query.get("online") in ("true", "false"):
                    agents = [row for row in agents if row["online"] == (query["online"] == "true")]
                limit, offset = int(query.get("limit", 100)), int(query.get("offset", 0))
                if not 1 <= limit <= 200 or offset < 0:
                    raise HTTPException(422, "Invalid directory page")
                return {"agents": agents[offset:offset + limit], "total": len(agents), "limit": limit, "offset": offset}
            if path == "/agents/discover" and method == "GET":
                return discover_agents(query)
            if path == "/agents/me" and method == "PATCH":
                patch = AgentCardPatch(**payload)
                fields = patch.model_dump(exclude_unset=True)
                if not fields:
                    raise HTTPException(422, "Provide description and/or capabilities")
                return public_agent(store.update_agent_card(identifier, fields), time.time())
            if path == "/inbox" and method == "GET":
                return page_mail(identifier, "inbox", {"unread_only": True, **query})
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

    def public_agent(row, now, profile=None):
        profile = profile or (store.profile_get(row.get("profile_id")) if row.get("profile_id") else {}) or {}
        result = {key: row[key] for key in ("id", "name", "kind", "project", "created_at", "last_seen")}
        result.update(instance_id=row["id"], profile_id=row.get("profile_id"),
                      instance_name=row.get("instance_name") or row["name"],
                      description=row.get("description") or profile.get("description", ""),
                      capabilities=row.get("capabilities") or profile.get("capabilities", []))
        result["online"] = row["last_seen"] >= now - online_seconds
        return result

    def directory_agents(now=None):
        now = now or time.time()
        profiles = {row["id"]: row for row in store.profiles_all()}
        return [public_agent(row, now, profiles.get(row.get("profile_id"))) for row in store.all_agents()]

    def discover_agents(query):
        def fold(value):
            return "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                           if not unicodedata.combining(char))
        text = fold(query.get("q", "").strip())
        capability = fold(query.get("capability", "").strip())
        online = query.get("online")
        terms = [part for part in text.split() if part]
        now = time.time()
        matches = []
        for agent in directory_agents(now):
            if online in ("true", "false") and agent["online"] != (online == "true"):
                continue
            caps = agent["capabilities"]
            matched_caps = [item for item in caps if capability and capability in fold(item)]
            if capability and not matched_caps:
                continue
            searchable = fold(" ".join([agent["name"], agent["instance_name"], agent["kind"],
                                         agent["project"], agent["description"], *caps]))
            matched_terms = [term for term in terms if term in searchable]
            if terms and len(matched_terms) != len(terms):
                continue
            score = 4 * len(matched_caps) + sum(3 if term in fold(" ".join(caps)) else
                                                 2 if term in fold(agent["description"]) else 1
                                                 for term in terms) + int(agent["online"])
            matches.append({**agent, "relevance": score,
                            "matched_capabilities": matched_caps or [item for item in caps
                                if any(term in fold(item) for term in terms)]})
        matches.sort(key=lambda agent: (-agent["relevance"], -agent["last_seen"], agent["id"]))
        limit = int(query.get("limit", 20))
        if not 1 <= limit <= 100:
            raise HTTPException(422, "Discovery limit must be 1–100")
        return {"agents": matches[:limit], "total": len(matches), "query": text,
                "capability": capability, "scope": "authenticated server directory; descriptions and capabilities are peer supplied"}

    def create_registration(body):
        identifier, token, now = str(uuid4()), secrets.token_urlsafe(32), time.time()
        row = dict(id=identifier, name=body.name, kind=body.kind, project=body.project,
                   description=body.description, capabilities=body.capabilities,
                   token_hash=hashlib.sha256(token.encode()).hexdigest(), created_at=now, last_seen=now)
        store.register(row)
        return {"agent": public_agent(row, now), "token": token}

    def public_profile(row):
        return {key: row.get(key, [] if key == "capabilities" else "") for key in
                ("id", "name", "kind", "project", "created_at", "description", "capabilities")}

    def profiles_directory():
        agents = store.all_agents()
        return {"profiles": [{**public_profile(row),
                              "instances_count": sum(a.get("profile_id") == row["id"] for a in agents)}
                             for row in store.profiles_all()]}

    def profile_instances(profile_id):
        profile = store.profile_get(profile_id)
        if not profile:
            raise HTTPException(404, "Profile not found")
        return {"profile_id": profile_id, "profile": public_profile(profile),
                "instances": [public_agent(row, time.time(), profile)
                for row in store.all_agents() if row.get("profile_id") == profile_id]}

    def new_profile(identifier, body):
        profile, token = store.create_profile(identifier, body.name, body.kind, body.project,
                                              body.description, body.capabilities)
        return {"profile": public_profile(profile), "enrollment_token": token,
                "instance": public_agent(store.get_agent(identifier), time.time()),
                "token_scope": "enroll and retire instances; cannot read inboxes"}

    def page_mail(identifier, box, query):
        try:
            params = MailQuery(**query)
        except ValidationError:
            raise HTTPException(422, "Invalid mailbox filters, cursor or limit")
        return store.mail_page(identifier, box, **params.model_dump())

    def inbox_summary_for(identifier):
        result = store.inbox_count(identifier)
        page = page_mail(identifier, "inbox", {"unread_only": True, "limit": 20})
        preview = [{key: row[key] for key in ("id", "subject", "thread_id", "status", "from_agent_id",
                   "created_at_iso", "message_type", "attachment_count", "has_images", "has_files")} for row in page["messages"]]
        return {**result, "instance": public_agent(store.get_agent(identifier), time.time()),
                "preview": preview, "has_more": page["has_more"], "checked_at": time.time()}

    def workflow_bundle():
        root = FilePath(__file__).parent
        return {"version": APP_VERSION, "skill": (root / "skills/agent-mailbox/SKILL.md").read_text(encoding="utf-8"),
                "persistent_instructions": (root / "docs/AGENT-INSTRUCTIONS.md").read_text(encoding="utf-8"),
                "guide_url": "/guide/operations", "scope": "task boundaries while the host has a model turn"}

    def reply_to(identifier, message_id, body):
        original = store.get_message(identifier, message_id)
        if original["to_agent_id"] != identifier:
            raise HTTPException(403, "Only the addressed instance can answer this request")
        if not secrets.compare_digest(body.request_fingerprint, original["request_fingerprint"]):
            raise HTTPException(409, "Reply context does not match this request; read it again by ID")
        message = Message(to_agent_id=original["from_agent_id"], in_reply_to=message_id, text=body.text,
                          subject=original["subject"], attachments=body.attachments, idempotency_key=body.idempotency_key)
        attachments = prepare_attachments(message.attachments)
        if not message.text.strip() and not attachments:
            raise HTTPException(422, "Reply needs text or an attachment")
        sent = store.send(identifier, message, attachments, finalize={"status": body.status})
        request = store.get_message(identifier, message_id)
        acknowledged = body.status == "completed"
        return {"reply": sent, "request": request, "acknowledged": acknowledged}

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
        agents = directory_agents(now)
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

    @app.get("/adapters.md", response_class=PlainTextResponse, include_in_schema=False)
    def adapter_instructions():
        path = FilePath(__file__).parent / "adapters" / "README.md"
        return PlainTextResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/connect-agent-prompt.md", response_class=PlainTextResponse, include_in_schema=False)
    def connect_agent_prompt():
        path = FilePath(__file__).parent / "docs" / "CONNECT-AGENT-PROMPT.md"
        return PlainTextResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/skill.md", response_class=PlainTextResponse, include_in_schema=False)
    def agent_mailbox_skill():
        path = FilePath(__file__).parent / "skills" / "agent-mailbox" / "SKILL.md"
        return PlainTextResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/workflow")
    def get_workflow_bundle():
        return JSONResponse(workflow_bundle(), headers={"Cache-Control": "no-store"})

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

    @app.get("/capabilities")
    def capabilities():
        return {"version": APP_VERSION, "profiles": True, "private_instance_inboxes": True,
                "bound_replies": True, "transactional_final_reply": True, "idempotent_delivery": True,
                "peer_discovery": {"enabled": True, "agent_card_description_chars": 500,
                                   "capabilities_per_card": 20, "directory_scope": "authenticated agents on this server"},
                "inbox_wait": {"enabled": True, "max_timeout_seconds": 25, "slots_per_process": 12,
                               "scope": "new unread messages for the authenticated instance only",
                               "mode": "opt-in; an open request can keep hosted compute active"},
                "attachments": {"max_count": 3, "total_bytes": MAX_ATTACHMENT_BYTES},
                "model_wake": "host-specific; event delivery does not itself start a model turn"}

    @app.post("/agents", status_code=201)
    def register(body: AgentRegistration, identity: Identity):
        if not identity["admin"]:
            raise HTTPException(403, "Registration requires the admin key")
        return create_registration(body)

    @app.get("/stats")
    def stats(identity: Identity):
        return {**store.stats(time.time() - online_seconds), "online_window_seconds": online_seconds}

    @app.get("/profiles")
    def list_profiles(identity: Identity):
        return profiles_directory()

    @app.post("/profiles", status_code=201)
    def create_profile(body: AgentRegistration, identity: Identity):
        return JSONResponse(new_profile(agent_id(identity), body), status_code=201,
                            headers={"Cache-Control": "no-store"})

    @app.patch("/profiles/{profile_id}")
    def update_profile_card(profile_id: str, body: ProfileCardPatch, credentials: Credential):
        store.authorize_profile(profile_id, credentials.credentials if credentials else "")
        fields = body.model_dump(exclude_unset=True)
        fields = {key: value if value is not None else ("" if key == "description" else [])
                  for key, value in fields.items()}
        if not fields:
            raise HTTPException(422, "Provide description and/or capabilities")
        return public_profile(store.update_profile_card(profile_id, fields))

    @app.patch("/profiles/{profile_id}/card")
    def update_member_profile_card(profile_id: str, body: ProfileCardPatch, identity: Identity):
        identifier = agent_id(identity)
        instance = store.get_agent(identifier)
        if not instance or instance.get("profile_id") != profile_id:
            raise HTTPException(403, "Only an instance in this profile can update its shared card")
        fields = body.model_dump(exclude_unset=True)
        fields = {key: value if value is not None else ("" if key == "description" else [])
                  for key, value in fields.items()}
        if not fields:
            raise HTTPException(422, "Provide description and/or capabilities")
        return public_profile(store.update_profile_card(profile_id, fields))

    @app.get("/profiles/{profile_id}/instances")
    def get_instances(profile_id: str, identity: Identity):
        return profile_instances(profile_id)

    @app.delete("/profiles/{profile_id}")
    def delete_profile(profile_id: str, credentials: Credential):
        store.authorize_profile(profile_id, credentials.credentials if credentials else "")
        store.delete_empty_profile(profile_id)
        return {"profile_id": profile_id, "deleted": True}

    @app.get("/profiles/{profile_id}/activity")
    def get_profile_activity(profile_id: str, identity: Identity):
        return store.profile_activity(identity.get("id"), profile_id, identity["admin"])

    @app.post("/profiles/{profile_id}/instances", status_code=201)
    def enroll_instance(profile_id: str, body: InstanceRegistration, credentials: Credential):
        profile = store.authorize_profile(profile_id, credentials.credentials if credentials else "")
        if body.session_key:
            identifier = str(uuid5(UUID(profile_id), body.session_key))
            token = hmac.new(credentials.credentials.encode(), identifier.encode(), hashlib.sha256).hexdigest()
        else:
            identifier, token = str(uuid4()), secrets.token_urlsafe(32)
        now = time.time()
        row = dict(id=identifier, name=body.instance_name, kind=profile["kind"],
                   project=body.project or profile["project"], created_at=now, last_seen=now,
                   description=body.description, capabilities=body.capabilities,
                   token_hash=hashlib.sha256(token.encode()).hexdigest())
        created = store.enroll_profile_instance(profile_id, row)
        return JSONResponse({"agent": public_agent(store.get_agent(identifier), time.time()),
                             "token": token, "resumed": not created}, status_code=201,
                            headers={"Cache-Control": "no-store"})

    @app.delete("/profiles/{profile_id}/instances/{instance_id}")
    def retire_instance(profile_id: str, instance_id: str, credentials: Credential):
        store.authorize_profile(profile_id, credentials.credentials if credentials else "")
        instance = store.get_agent(instance_id)
        if not instance or instance.get("profile_id") != profile_id:
            raise HTTPException(404, "Instance not found in profile")
        store.unregister(instance_id)
        return {"instance_id": instance_id, "deleted": True}

    @app.get("/agents/discover")
    def discover_agent_cards(identity: Identity, q: str = Query("", max_length=200),
                             capability: str = Query("", max_length=64), online: bool | None = None,
                             limit: int = Query(20, ge=1, le=100)):
        query = {"q": q, "capability": capability, "limit": limit}
        if online is not None:
            query["online"] = str(online).lower()
        return discover_agents(query)

    @app.get("/agents")
    def list_agents(identity: Identity, online: bool | None = None,
                    limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        now = time.time()
        agents = directory_agents(now)
        if online is not None:
            agents = [row for row in agents if row["online"] == online]
        return {"agents": agents[offset:offset + limit], "total": len(agents), "limit": limit, "offset": offset}

    @app.get("/agents/me")
    def me(identity: Identity):
        return public_agent(store.get_agent(agent_id(identity)), time.time())

    @app.patch("/agents/me")
    def update_my_card(body: AgentCardPatch, identity: Identity):
        fields = body.model_dump(exclude_unset=True)
        fields = {key: value if value is not None else ("" if key == "description" else [])
                  for key, value in fields.items()}
        if not fields:
            raise HTTPException(422, "Provide description and/or capabilities")
        row = store.update_agent_card(agent_id(identity), fields)
        return public_agent(row, time.time())

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

    @app.post("/messages/{message_id}/reply", status_code=201)
    def reply_message(message_id: Annotated[int, Path(gt=0, le=MAX_MESSAGE_ID)], body: Reply, identity: Identity):
        return reply_to(agent_id(identity), message_id, body)

    @app.patch("/messages/{message_id}/status")
    def set_message_status(message_id: Annotated[int, Path(gt=0, le=MAX_MESSAGE_ID)], body: RequestStatus,
                           identity: Identity):
        return store.update_status(agent_id(identity), message_id, body.status, body.note)

    @app.get("/threads/{message_id}")
    def get_thread(message_id: Annotated[int, Path(gt=0, le=MAX_MESSAGE_ID)], identity: Identity,
                   after_id: int = Query(0, ge=0, le=MAX_MESSAGE_ID), limit: int = Query(100, ge=1, le=200)):
        return store.thread_messages(agent_id(identity), message_id, after_id, limit)

    @app.get("/outbox")
    def get_outbox(identity: Identity, after_id: int = Query(0, ge=0, le=MAX_MESSAGE_ID),
                   limit: int = Query(50, ge=1, le=200), status: str | None = None,
                   thread_id: int | None = Query(None, gt=0), has_attachments: bool | None = None):
        return page_mail(agent_id(identity), "outbox", dict(after_id=after_id, limit=limit, status=status,
                         thread_id=thread_id, has_attachments=has_attachments))

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
        ascii_filename = filename.encode("ascii", errors="ignore").decode() or "attachment"
        return Response(content=attachment["data"], media_type=attachment["media_type"],
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename, safe='')}"})

    @app.get("/inbox/count")
    def inbox_count(identity: Identity):
        return store.inbox_count(agent_id(identity))

    @app.get("/inbox/summary")
    def get_inbox_summary(identity: Identity):
        return inbox_summary_for(agent_id(identity))

    @app.get("/inbox/wait")
    def wait_inbox(identity: Identity, after_id: int = Query(0, ge=0, le=MAX_MESSAGE_ID),
                   timeout: int = Query(25, ge=1, le=25)):
        return wait_mail(agent_id(identity), after_id, timeout)

    @app.get("/inbox")
    def inbox(identity: Identity, unread_only: bool = True,
              after_id: int = Query(0, ge=0, le=MAX_MESSAGE_ID), limit: int = Query(50, ge=1, le=200),
              status: str | None = None, thread_id: int | None = Query(None, gt=0),
              has_attachments: bool | None = None):
        return page_mail(agent_id(identity), "inbox", dict(after_id=after_id, limit=limit,
                         unread_only=unread_only, status=status, thread_id=thread_id,
                         has_attachments=has_attachments))

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

