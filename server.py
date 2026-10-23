"""A shared, persistent mailbox. Agents poll it using ordinary HTTP requests."""

import hashlib
import base64
import binascii
import os
import re
import hmac
import ipaddress
import threading
import secrets
import time
import unicodedata
from html import escape
from contextvars import Token
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4, uuid5, UUID

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse, Response, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, AliasChoices, ValidationError, field_validator
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from stores import FirestoreStore, SQLiteStore

MAX_MESSAGE_ID = 2**63 - 1
MAX_AGENT_API_KEYS = 25
PASSWORD_HASH_MEMORY_KIB = 19 * 1024
PASSWORD_HASH_ITERATIONS = 2
ACCOUNT_SESSION_SECONDS = 12 * 60 * 60
PASSWORD_KDF_SLOTS = threading.BoundedSemaphore(4)
APP_VERSION = FilePath(__file__).with_name("VERSION").read_text(encoding="ascii").strip()


class PasswordKdfBusy(Exception):
    pass


def password_hash(password):
    if not PASSWORD_KDF_SLOTS.acquire(timeout=2):
        raise PasswordKdfBusy
    try:
        salt = secrets.token_bytes(16)
        digest = Argon2id(salt=salt, length=32, iterations=PASSWORD_HASH_ITERATIONS,
                          lanes=1, memory_cost=PASSWORD_HASH_MEMORY_KIB).derive(password.encode("utf-8"))
        return f"argon2id$v=19$m={PASSWORD_HASH_MEMORY_KIB},t={PASSWORD_HASH_ITERATIONS},p=1${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"
    finally:
        PASSWORD_KDF_SLOTS.release()


def verify_password(password, encoded):
    if not PASSWORD_KDF_SLOTS.acquire(timeout=2):
        raise PasswordKdfBusy
    try:
        scheme, version, parameters, salt, expected = encoded.split("$", 4)
        if (scheme != "argon2id" or version != "v=19" or
                parameters != f"m={PASSWORD_HASH_MEMORY_KIB},t={PASSWORD_HASH_ITERATIONS},p=1"):
            return False
        actual = Argon2id(salt=base64.urlsafe_b64decode(salt), length=32,
                          iterations=PASSWORD_HASH_ITERATIONS, lanes=1,
                          memory_cost=PASSWORD_HASH_MEMORY_KIB).derive(password.encode("utf-8"))
        return secrets.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False
    finally:
        PASSWORD_KDF_SLOTS.release()


def request_password_hash(password):
    try:
        return password_hash(password)
    except PasswordKdfBusy:
        raise HTTPException(429, "Password service is busy; retry shortly", headers={"Retry-After": "2"})


def request_verify_password(password, encoded):
    try:
        return verify_password(password, encoded)
    except PasswordKdfBusy:
        raise HTTPException(429, "Password service is busy; retry shortly", headers={"Retry-After": "2"})


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


class ControlUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=80)
    role: Literal["observer", "operator"] = "observer"


class OwnerSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setup_key: str = Field(default="", max_length=256)
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,39}$")
    password: str = Field(min_length=12, max_length=256)


class AccountLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=256)


class AccessKeyLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_key: str = Field(min_length=16, max_length=256)


class TeamAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,39}$")
    password: str = Field(min_length=12, max_length=256)
    role: Literal["operator", "observer"]


class AccountPasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class AccountPasswordRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=40)
    new_password: str = Field(min_length=12, max_length=256)


class AgentApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)


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
    local_first_run = os.environ.get("MAILBOX_LOCAL_FIRST_RUN") == "1" and backend == "sqlite"
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

    def secure_web_cookies(request):
        public_origin = os.environ.get("MAILBOX_PUBLIC_URL", "").strip()
        return (public_origin or str(request.base_url)).startswith("https://")

    def session_cookie_name(request):
        return "__Host-agent-mailbox-session" if secure_web_cookies(request) else "agent-mailbox-session"

    def csrf_cookie_name(request):
        return "__Host-agent-mailbox-csrf" if secure_web_cookies(request) else "agent-mailbox-csrf"

    def request_session_token(request):
        return request.cookies.get(session_cookie_name(request)) or request.cookies.get("agent-mailbox-session") or request.cookies.get("__Host-agent-mailbox-session")

    def local_first_owner_host(request):
        if not local_first_run or not request.client:
            return False
        try:
            if not ipaddress.ip_address(request.client.host).is_loopback:
                return False
        except ValueError:
            return False
        host_header = request.headers.get("host", "")
        host_name = urlsplit("//" + host_header).hostname or ""
        if host_name.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host_name).is_loopback
        except ValueError:
            return False

    def local_first_owner_request(request):
        if not local_first_owner_host(request):
            return False
        origin = request.headers.get("origin", "")
        host_header = request.headers.get("host", "")
        try:
            parsed_origin = urlsplit(origin)
            origin_host = parsed_origin.hostname or ""
            origin_is_loopback = origin_host.lower() == "localhost" or ipaddress.ip_address(origin_host).is_loopback
        except ValueError:
            origin_is_loopback = False
            parsed_origin = urlsplit("")
        return bool(origin and host_header and origin_is_loopback and
                    parsed_origin.scheme == request.url.scheme and
                    parsed_origin.netloc.casefold() == host_header.casefold())

    def set_session_cookies(response, request, session_token, csrf_token):
        secure = secure_web_cookies(request)
        response.set_cookie(session_cookie_name(request), session_token, max_age=ACCOUNT_SESSION_SECONDS,
                            httponly=True, secure=secure, samesite="strict", path="/")
        response.set_cookie(csrf_cookie_name(request), csrf_token, max_age=ACCOUNT_SESSION_SECONDS,
                            httponly=False, secure=secure, samesite="strict", path="/")

    def clear_session_cookies(response, request):
        secure = secure_web_cookies(request)
        for name in {session_cookie_name(request), csrf_cookie_name(request),
                     "agent-mailbox-session", "agent-mailbox-csrf",
                     "__Host-agent-mailbox-session", "__Host-agent-mailbox-csrf"}:
            response.delete_cookie(name, path="/", secure=secure, httponly="session" in name,
                                   samesite="strict")

    def web_session_by_hash(token_hash, now):
        account_session = store.account_session_by_hash(token_hash, now)
        if account_session:
            return account_session | {"session_type": "account", "auth_method": "password"}
        control_session = store.control_session_by_hash(token_hash, now)
        if not control_session:
            return None
        if (control_session.get("identity_type") == "owner_key" and
                not secrets.compare_digest(control_session.get("credential_hash", ""),
                                           hashlib.sha256(hub_key.encode()).hexdigest())):
            return None
        return control_session | {"session_type": "control", "auth_method": "access_key"}

    @app.middleware("http")
    async def private_response_headers(request, call_next):
        response = await call_next(request)
        if request.headers.get("authorization") or request.url.path.startswith(("/dashboard", "/auth/")):
            response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.middleware("http")
    async def account_csrf_guard(request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in {"/auth/login", "/auth/setup", "/auth/key-login"} and not request.headers.get("authorization"):
            session_token = request_session_token(request)
            if session_token:
                session = web_session_by_hash(hashlib.sha256(session_token.encode()).hexdigest(), time.time())
                csrf_header = request.headers.get("x-csrf-token", "")
                csrf_cookie = request.cookies.get(csrf_cookie_name(request), "") or request.cookies.get("agent-mailbox-csrf", "")
                if (not session or not csrf_header or not csrf_cookie or
                        not secrets.compare_digest(csrf_header, csrf_cookie) or
                        not secrets.compare_digest(hashlib.sha256(csrf_header.encode()).hexdigest(), session["csrf_hash"])):
                    return JSONResponse({"detail": "Invalid or missing CSRF token"}, status_code=403,
                                        headers={"Cache-Control": "no-store"})
        return await call_next(request)

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
        ("/brand-symbol.svg", "assets/brand-symbol.svg", "image/svg+xml"),
        ("/social-card.png", "assets/social-card.png", "image/png"),
        ("/use-cases", "use-cases.html", "text/html"), ("/use-cases.js", "use-cases.js", "application/javascript"),
        ("/mailbox-client.py", "mailbox_client.py", "text/x-python"),
        ("/agent-instructions.md", "docs/AGENT-INSTRUCTIONS.md", "text/plain"),
        ("/agent-bank.md", "docs/AGENT-MAILBOX-MASTER-PROMPT.md", "text/plain"),
    ]:
        app.add_api_route(url, public_file(filename, mime), methods=["GET"], include_in_schema=False)

    @app.get("/guide", include_in_schema=False)
    def guides_home():
        return RedirectResponse("/guide/overview")

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
        api_key = None if identifier else store.authenticate_agent_api_key(hashlib.sha256(token.encode()).hexdigest())
        if identifier is None and api_key:
            identifier = api_key["id"]
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

    def authenticate(credentials: Credential, request: Request):
        if credentials is None:
            session_token = request_session_token(request)
            if session_token:
                session = web_session_by_hash(hashlib.sha256(session_token.encode()).hexdigest(), time.time())
                if session:
                    subject_id = session.get("user_id") or session.get("control_user_id") or "owner-key"
                    common = {"id": subject_id, "username": session["username"],
                              "name": session["username"], "role": session.get("role", "owner"),
                              "account_session_id": session["id"], "csrf_hash": session["csrf_hash"],
                              "auth_method": session["auth_method"]}
                    return ({"admin": True} | common if common["role"] == "owner" else
                            {"admin": False, "control_user": True} | common)
            raise HTTPException(401, "Account session required", headers={"WWW-Authenticate": "Bearer"})
        token = credentials.credentials
        if secrets.compare_digest(token.encode(), hub_key.encode()):
            return {"admin": True}
        identifier = store.authenticate(hashlib.sha256(token.encode()).hexdigest())
        if identifier is not None:
            return {"admin": False, "id": identifier, "credential_type": "instance"}
        api_key = store.authenticate_agent_api_key(hashlib.sha256(token.encode()).hexdigest())
        if api_key is not None:
            return {"admin": False, "id": api_key["id"], "credential_type": "api_key",
                    "api_key_id": api_key["api_key_id"]}
        if identifier is None:
            user = store.control_user_by_hash(hashlib.sha256(token.encode()).hexdigest())
            if user:
                return {"admin": False, "control_user": True, "id": user["id"],
                        "name": user["name"], "role": user["role"]}
            raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
        return {"admin": False, "id": identifier, "credential_type": "instance"}

    def create_agent_api_key(identifier, name):
        token = "amb_live_" + secrets.token_urlsafe(32)
        row = {"id": str(uuid4()), "agent_id": identifier, "name": name,
               "key_prefix": token[:17], "token_hash": hashlib.sha256(token.encode()).hexdigest(),
               "created_at": time.time(), "last_used_at": None, "revoked_at": None}
        store.create_agent_api_key(row)
        return {"key": {key: row[key] for key in ("id", "name", "key_prefix", "created_at", "last_used_at")},
                "token": token, "warning": "Copy this key now. It cannot be shown again."}

    def public_agent_api_key(row):
        return {key: row.get(key) for key in
                ("id", "name", "key_prefix", "created_at", "last_used_at", "revoked_at")}

    Identity = Annotated[dict, Depends(authenticate)]

    dummy_password_hash = password_hash(secrets.token_urlsafe(24))

    def account_login_rate_key(username_norm, request):
        address = request.client.host if request.client else "unknown"
        return hmac.new(hub_key.encode(), f"{username_norm}|{address}".encode(), hashlib.sha256).hexdigest()

    def current_web_session(request):
        session_token = request_session_token(request)
        if not session_token:
            raise HTTPException(401, "Sign in to continue")
        session = web_session_by_hash(hashlib.sha256(session_token.encode()).hexdigest(), time.time())
        if not session:
            raise HTTPException(401, "Account session expired; sign in again")
        return session

    def current_account_session(request):
        session = current_web_session(request)
        if session.get("session_type") != "account":
            raise HTTPException(403, "This action requires a username/password session")
        return session

    def establish_account_session(user, request, status_code=200):
        now = time.time()
        session_token, csrf_token = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        session = {"id": str(uuid4()), "user_id": user["id"],
                   "token_hash": hashlib.sha256(session_token.encode()).hexdigest(),
                   "csrf_hash": hashlib.sha256(csrf_token.encode()).hexdigest(),
                   "created_at": now, "expires_at": now + ACCOUNT_SESSION_SECONDS, "last_seen": now,
                   "revoked_at": None}
        store.create_account_session(session)
        response = JSONResponse({"user": {"id": user["id"], "username": user["username"], "role": user.get("role", "owner")},
                                 "expires_at": session["expires_at"]}, status_code=status_code,
                                headers={"Cache-Control": "no-store"})
        set_session_cookies(response, request, session_token, csrf_token)
        return response

    def establish_control_session(control_identity, request):
        now = time.time()
        session_token, csrf_token = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        session = {"id": str(uuid4()), "control_user_id": control_identity.get("id"),
                   "identity_type": control_identity["identity_type"],
                   "username": control_identity["username"], "role": control_identity["role"],
                   "credential_hash": control_identity.get("credential_hash"),
                   "token_hash": hashlib.sha256(session_token.encode()).hexdigest(),
                   "csrf_hash": hashlib.sha256(csrf_token.encode()).hexdigest(),
                   "created_at": now, "expires_at": now + ACCOUNT_SESSION_SECONDS,
                   "last_seen": now, "revoked_at": None}
        store.create_control_session(session)
        response = JSONResponse({"user": {"id": control_identity.get("id", "owner-key"),
                                           "username": control_identity["username"],
                                           "role": control_identity["role"]},
                                 "auth_method": "access_key", "expires_at": session["expires_at"]},
                                headers={"Cache-Control": "no-store"})
        set_session_cookies(response, request, session_token, csrf_token)
        return response

    @app.get("/auth/status")
    def account_status(request: Request):
        exists = store.owner_account_exists()
        return JSONResponse({"setup_required": not exists,
                             "local_setup_available": not exists and local_first_owner_host(request)},
                            headers={"Cache-Control": "no-store"})

    @app.post("/auth/setup", status_code=201)
    def setup_owner_account(body: OwnerSetup, request: Request):
        if store.owner_account_exists():
            raise HTTPException(409, "The first owner account is already set up")
        has_setup_key = bool(body.setup_key) and secrets.compare_digest(body.setup_key.encode(), hub_key.encode())
        if not has_setup_key and not local_first_owner_request(request):
            raise HTTPException(401, "Invalid one-time setup secret")
        now = time.time()
        username = body.username.strip()
        user = {"id": str(uuid4()), "username": username, "username_norm": username.casefold(),
                "password_hash": request_password_hash(body.password), "role": "owner", "created_at": now,
                "last_login_at": now, "disabled_at": None}
        try:
            store.create_owner_account(user)
        except HTTPException:
            raise
        return establish_account_session(user, request, status_code=201)

    @app.post("/auth/login")
    def account_login(body: AccountLogin, request: Request):
        username_norm = body.username.strip().casefold()
        rate_key = account_login_rate_key(username_norm, request)
        if not store.account_login_allowed(rate_key, time.time()):
            raise HTTPException(429, "Too many failed sign-in attempts. Try again in 15 minutes.",
                                headers={"Retry-After": "900"})
        account = store.account_by_username(username_norm)
        encoded = account["password_hash"] if account else dummy_password_hash
        valid = request_verify_password(body.password, encoded) and account is not None
        store.record_account_login(rate_key, time.time(), valid)
        if not valid:
            raise HTTPException(401, "Username or password is incorrect")
        seen = time.time()
        store.update_account_login(account["id"], seen)
        account["last_login_at"] = seen
        return establish_account_session(account, request)

    @app.post("/auth/key-login")
    def legacy_access_key_login(body: AccessKeyLogin, request: Request):
        digest = hashlib.sha256(body.access_key.encode()).hexdigest()
        if secrets.compare_digest(body.access_key.encode(), hub_key.encode()):
            identity = {"identity_type": "owner_key", "username": "Propietario",
                        "role": "owner", "credential_hash": digest}
        else:
            user = store.control_user_by_hash(digest)
            if not user:
                raise HTTPException(401, "Clave de usuario incorrecta, revocada o incompatible")
            identity = {"identity_type": "control_user", "id": user["id"],
                        "username": user["name"], "role": user["role"]}
        return establish_control_session(identity, request)

    @app.get("/auth/session")
    def account_session_info(request: Request):
        session = current_web_session(request)
        return JSONResponse({"user": {"id": session.get("user_id") or session.get("control_user_id") or "owner-key",
                                       "username": session["username"], "role": session.get("role", "owner")},
                             "auth_method": session["auth_method"],
                             "expires_at": session["expires_at"]}, headers={"Cache-Control": "no-store"})

    @app.post("/auth/password")
    def change_account_password(body: AccountPasswordChange, request: Request):
        session = current_account_session(request)
        account = store.account_by_username(session["username"].casefold())
        if not account or not request_verify_password(body.current_password, account["password_hash"]):
            raise HTTPException(401, "Current password is incorrect")
        if not store.update_account_password(account["id"], request_password_hash(body.new_password), time.time()):
            raise HTTPException(404, "Account is unavailable")
        account["last_login_at"] = time.time()
        store.update_account_login(account["id"], account["last_login_at"])
        return establish_account_session(account, request)

    @app.post("/auth/recover")
    def recover_account_password(body: AccountPasswordRecovery, credentials: Credential):
        if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), hub_key.encode()):
            raise HTTPException(403, "Password recovery requires the owner emergency key")
        account = store.account_by_username(body.username.strip().casefold())
        if not account or not store.update_account_password(account["id"], request_password_hash(body.new_password), time.time()):
            raise HTTPException(404, "Account not found or disabled")
        store.record_control_event("owner-emergency-key", "account_password_recovered", account["id"])
        return {"username": account["username"], "password_reset": True,
                "sessions_revoked": True, "next_step": "Sign in again with the new password"}

    @app.post("/auth/logout")
    def account_logout(request: Request):
        session = current_web_session(request)
        if session.get("session_type") == "account":
            store.revoke_account_session(session["id"], time.time())
        else:
            store.revoke_control_session(session["id"], time.time())
        response = JSONResponse({"logged_out": True}, headers={"Cache-Control": "no-store"})
        clear_session_cookies(response, request)
        return response

    def agent_id(identity):
        if identity["admin"] or identity.get("control_user"):
            raise HTTPException(403, "Use an agent token for messaging and presence")
        return identity["id"]

    def can_manage_agents(identity):
        return identity.get("admin", False) or (identity.get("control_user") and identity.get("role") == "operator")

    def require_admin(identity):
        if not identity.get("admin"):
            raise HTTPException(403, "Only the owner key can manage operator accounts and audit records")

    def control_user_view(row):
        return {key: row.get(key) for key in ("id", "name", "role", "created_by", "created_at", "last_seen", "revoked_at")}

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
                "master_prompt": (root / "docs/AGENT-MAILBOX-MASTER-PROMPT.md").read_text(encoding="utf-8"),
                "persistent_instructions": (root / "docs/AGENT-INSTRUCTIONS.md").read_text(encoding="utf-8"),
                "agent_setup_guide": (root / "docs/AGENT-SETUP.md").read_text(encoding="utf-8"),
                "setup_guide_url": "/agent-setup.md", "adapter_prompt_url": "/adapter-build-prompt.md",
                "guide_url": "/guide/start", "scope": "task boundaries while the host has a model turn"}

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

    def marketing_page(filename, request):
        origin = (os.environ.get("MAILBOX_PUBLIC_URL") or str(request.base_url)).rstrip("/")
        content = (FilePath(__file__).parent / filename).read_text(encoding="utf-8-sig")
        return HTMLResponse(content.replace("{{PUBLIC_ORIGIN}}", escape(origin, quote=True)), headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        })

    @app.get("/", include_in_schema=False)
    def welcome(request: Request):
        return marketing_page("connect.html", request)

    @app.get("/en", include_in_schema=False)
    def welcome_english(request: Request):
        return marketing_page("connect-en.html", request)

    @app.get("/es", include_in_schema=False)
    def welcome_spanish():
        return RedirectResponse("/")

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
        if not identity["admin"] and not identity.get("control_user"):
            raise HTTPException(403, "El panel requiere una clave de administración u operador")
        now = time.time()
        agents = directory_agents(now)
        messages = store.recent_messages(15)
        if identity.get("role") == "observer":
            messages = [{key: row.get(key) for key in
                         ("id", "from_agent_id", "to_agent_id", "created_at", "acknowledged_at",
                          "thread_id", "status", "message_type", "attachment_count", "has_images", "has_files")}
                        for row in messages]
        permissions = {"is_admin": identity["admin"], "role": "owner" if identity["admin"] else identity.get("role"),
                       "can_manage_agents": can_manage_agents(identity),
                       "can_manage_users": identity["admin"], "can_read_message_content": identity["admin"] or identity.get("role") == "operator",
                       "can_delete_messages": identity["admin"]}
        return {"stats": {**store.stats(now - online_seconds), "online_window_seconds": online_seconds},
                "agents": agents, "messages": messages, "permissions": permissions, "updated_at": now}

    @app.get("/dashboard/users")
    def dashboard_users(identity: Identity):
        require_admin(identity)
        return {"users": [control_user_view(row) for row in store.all_control_users()],
                "audit": store.control_audit(100)}

    @app.post("/dashboard/users", status_code=201)
    def create_dashboard_user(body: ControlUserCreate, identity: Identity):
        require_admin(identity)
        if sum(row.get("revoked_at") is None for row in store.all_control_users()) >= 100:
            raise HTTPException(409, "Control-key limit reached; revoke an unused key first")
        token = "amk_" + secrets.token_urlsafe(32)
        row = dict(id=str(uuid4()), name=body.name, role=body.role,
                   token_hash=hashlib.sha256(token.encode()).hexdigest(),
                   created_by="owner", created_at=time.time(), last_seen=None, revoked_at=None)
        store.create_control_user(row)
        return JSONResponse({"user": control_user_view(row), "access_key": token,
                             "message": "This key is shown only once. Store it securely."},
                            status_code=201, headers={"Cache-Control": "no-store"})

    @app.delete("/dashboard/users/{user_id}")
    def revoke_dashboard_user(user_id: str, identity: Identity):
        require_admin(identity)
        if not store.revoke_control_user(user_id):
            raise HTTPException(404, "User key not found or already revoked")
        return {"id": user_id, "revoked": True}

    @app.get("/dashboard/accounts")
    def dashboard_accounts(identity: Identity):
        require_admin(identity)
        return {"accounts": store.all_account_users()}

    @app.post("/dashboard/accounts", status_code=201)
    def create_dashboard_account(body: TeamAccountCreate, identity: Identity):
        require_admin(identity)
        active = [row for row in store.all_account_users() if row.get("disabled_at") is None]
        if len(active) >= 100:
            raise HTTPException(409, "Account limit reached; disable an unused account first")
        username = body.username.strip()
        row = {"id": str(uuid4()), "username": username, "username_norm": username.casefold(),
               "password_hash": request_password_hash(body.password), "role": body.role, "created_at": time.time(),
               "last_login_at": None, "disabled_at": None}
        store.create_team_account(row)
        store.record_control_event(identity.get("id", "owner"), "account_created", row["id"])
        return {key: row[key] for key in ("id", "username", "role", "created_at", "last_login_at", "disabled_at")}

    @app.delete("/dashboard/accounts/{user_id}")
    def disable_dashboard_account(user_id: str, identity: Identity):
        require_admin(identity)
        if not store.disable_team_account(user_id, time.time()):
            raise HTTPException(404, "Non-owner account not found or already disabled")
        store.record_control_event(identity.get("id", "owner"), "account_disabled", user_id)
        return {"id": user_id, "disabled": True}

    @app.get("/dashboard/audit")
    def dashboard_audit(identity: Identity, limit: int = Query(100, ge=1, le=200)):
        require_admin(identity)
        return {"events": store.control_audit(limit)}

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

    @app.get("/agent-setup.md", response_class=PlainTextResponse, include_in_schema=False)
    def agent_setup_guide():
        path = FilePath(__file__).parent / "docs" / "AGENT-SETUP.md"
        return PlainTextResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/adapter-build-prompt.md", response_class=PlainTextResponse, include_in_schema=False)
    def adapter_build_prompt():
        path = FilePath(__file__).parent / "docs" / "ADAPTER-BUILD-PROMPT.md"
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

    @app.get("/mailbox-hook.py", include_in_schema=False)
    def mailbox_hook_script():
        return FileResponse(FilePath(__file__).parent / "adapters" / "mailbox_hook.py",
                            media_type="text/x-python", headers={"Cache-Control": "no-store",
                                                                  "X-Content-Type-Options": "nosniff"})

    @app.get("/mailbox-wake.py", include_in_schema=False)
    def mailbox_wake_script():
        return FileResponse(FilePath(__file__).parent / "adapters" / "mailbox_wake.py",
                            media_type="text/x-python", headers={"Cache-Control": "no-store",
                                                                  "X-Content-Type-Options": "nosniff"})

    @app.get("/mailbox-spool.py", include_in_schema=False)
    def mailbox_spool_script():
        return FileResponse(FilePath(__file__).parent / "adapters" / "mailbox_spool.py",
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
                "control_roles": ["owner", "operator", "observer"], "local_file_bank": True,
                "peer_discovery": {"enabled": True, "agent_card_description_chars": 500,
                                   "capabilities_per_card": 20, "directory_scope": "authenticated agents on this server"},
                "inbox_wait": {"enabled": True, "max_timeout_seconds": 25, "slots_per_process": 12,
                               "scope": "new unread messages for the authenticated instance only",
                               "mode": "opt-in; an open request can keep hosted compute active"},
                "attachments": {"max_count": 3, "total_bytes": MAX_ATTACHMENT_BYTES},
                "model_wake": "host-specific; event delivery does not itself start a model turn"}

    @app.post("/agents", status_code=201)
    def register(body: AgentRegistration, identity: Identity):
        if not can_manage_agents(identity):
            raise HTTPException(403, "Agent registration requires an owner or operator key")
        result = create_registration(body)
        if identity.get("control_user"):
            store.record_control_event(identity["id"], "agent_registered", result["agent"]["id"])
        return result

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

    @app.get("/agents/me/api-keys")
    def list_agent_api_keys(identity: Identity):
        identifier = agent_id(identity)
        return {"keys": [public_agent_api_key(row) for row in store.agent_api_keys(identifier)],
                "can_manage": identity.get("credential_type") == "instance"}

    @app.post("/agents/me/api-keys", status_code=201)
    def issue_agent_api_key(body: AgentApiKeyCreate, identity: Identity):
        identifier = agent_id(identity)
        if identity.get("credential_type") != "instance":
            raise HTTPException(403, "Use the original instance token to create or revoke API keys")
        if sum(row.get("revoked_at") is None for row in store.agent_api_keys(identifier)) >= MAX_AGENT_API_KEYS:
            raise HTTPException(409, f"This instance already has {MAX_AGENT_API_KEYS} active API keys")
        return JSONResponse(create_agent_api_key(identifier, body.name), status_code=201,
                            headers={"Cache-Control": "no-store"})

    @app.delete("/agents/me/api-keys/{key_id}")
    def revoke_agent_api_key(key_id: str, identity: Identity):
        identifier = agent_id(identity)
        if identity.get("credential_type") != "instance":
            raise HTTPException(403, "Use the original instance token to create or revoke API keys")
        if not store.revoke_agent_api_key(identifier, key_id):
            raise HTTPException(404, "Active API key not found for this instance")
        return {"id": key_id, "revoked": True}

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
        if not can_manage_agents(identity):
            raise HTTPException(403, "Only an owner or operator can remove another agent")
        if not store.unregister(identifier):
            raise HTTPException(404, "Agent not found")
        if identity.get("control_user"):
            store.record_control_event(identity["id"], "agent_removed", identifier)
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

    @app.get("/mailbox-listener.py", include_in_schema=False)
    def universal_mailbox_listener():
        return FileResponse(FilePath(__file__).parent / "mailbox_listener.py",
                            media_type="text/x-python", headers={"Cache-Control": "no-store",
                                                                  "X-Content-Type-Options": "nosniff"})

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

