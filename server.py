"""A shared, persistent mailbox. Agents poll it using ordinary HTTP requests."""

import hashlib
import base64
import binascii
import os
import re
import hmac
import ipaddress
import json
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
from uuid import uuid4, uuid5, UUID, NAMESPACE_URL

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse, Response, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, AliasChoices, ValidationError, field_validator
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from stores import FirestoreStore, SQLiteStore
from collaboration import DEFAULT_WORKSPACE_ID
from onboarding import catalog as onboarding_catalog, core_archive, handoff as onboarding_handoff, resource_file, resource_content
from a2a_gateway import (A2A_VERSION, ROUTING_EXTENSION, agent_card as a2a_agent_card,
                         decode_page_token as a2a_decode_page_token,
                         encode_page_token as a2a_encode_page_token,
                         mailbox_id_from_task as a2a_mailbox_id_from_task,
                         mailbox_thread_from_context as a2a_thread_from_context,
                         mailbox_status_from_task_state as a2a_mailbox_status_from_task_state,
                         problem as a2a_problem, task_document as a2a_task_document)

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
    chat_path: str = Field(default="", max_length=300)
    # Obligatorio cuando el agente se registra con la clave de registro de una cuenta: debe
    # decir de quién es el token que le dieron. Con sesión del panel se deduce del dueño.
    owner_name: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    session_key: str | None = Field(default=None, min_length=1, max_length=200)

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
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = Field(default=None, max_length=20)
    chat_path: str | None = Field(default=None, max_length=300)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, values):
        return AgentRegistration.unique_capabilities(values) if values is not None else values


class ProfileCardPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    description: str | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = Field(default=None, max_length=20)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, values):
        return AgentRegistration.unique_capabilities(values) if values is not None else values


class AgentRename(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)


class ControlUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=80)
    role: Literal["observer", "operator"] = "observer"


class OwnerSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setup_key: str = Field(default="", max_length=256)
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,39}$")
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(default="", max_length=120)
    organization: str = Field(default="", max_length=120)
    project: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=500)


class AccountLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=256)


class AccessKeyLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_key: str = Field(min_length=16, max_length=256)


class WorkspaceSignup(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    invite_code: str = Field(min_length=20, max_length=128)
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,39}$")
    password: str = Field(min_length=12, max_length=256)
    workspace_name: str = Field(min_length=2, max_length=80)
    display_name: str = Field(default="", max_length=120)
    organization: str = Field(default="", max_length=120)
    project: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=500)


class MemberSignup(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    invite_code: str = Field(min_length=20, max_length=128)
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,39}$")
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(default="", max_length=120)
    organization: str = Field(default="", max_length=120)
    project: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=500)


class AccountSignup(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,39}$")
    password: str = Field(min_length=12, max_length=256)
    workspace_name: str = Field(min_length=2, max_length=80)
    display_name: str = Field(default="", max_length=120)
    organization: str = Field(default="", max_length=120)
    project: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=500)


class AccountProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    display_name: str | None = Field(default=None, max_length=120)
    organization: str | None = Field(default=None, max_length=120)
    project: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=500)


class WorkspaceInviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_in_hours: int = Field(default=168, ge=1, le=720)
    mode: Literal["workspace", "member"] = "workspace"


class WorkspaceConnectionAccept(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    invite_code: str = Field(min_length=20, max_length=128)


class WorkspaceConnectionAccept(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    invite_code: str = Field(min_length=20, max_length=128)


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
    delivery_mode: Literal["inbox", "wake"] = "inbox"
    wake_backlog: bool = False
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


class ListenerReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: int = Field(ge=5, le=300)
    wake_configured: bool = False


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
FIRESTORE_ATTACHMENT_BYTES = 8 * 1024 * 1024
BLOCKED_ATTACHMENT_SUFFIXES = {".ade", ".apk", ".app", ".bat", ".cmd", ".com", ".deb",
                               ".dll", ".dmg", ".exe", ".hta", ".img", ".iso", ".jar", ".msi",
                               ".ps1", ".rpm", ".scr", ".sh", ".sys", ".vbe", ".vbs"}
MAX_BROADCAST_RECIPIENTS = 50


def prepare_attachments(items, max_bytes=MAX_ATTACHMENT_BYTES):
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
        if total > max_bytes:
            raise HTTPException(413, f"Attachments exceed the {max_bytes // (1024 * 1024)} MiB per-message limit")
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
    attachment_limit = FIRESTORE_ATTACHMENT_BYTES if backend == "firestore" else MAX_ATTACHMENT_BYTES
    local_first_run = os.environ.get("MAILBOX_LOCAL_FIRST_RUN") == "1" and backend == "sqlite"
    local_instance_seed = os.environ.get("MAILBOX_LOCAL_INSTANCE_SEED") or hub_key
    if local_first_run and len(local_instance_seed) < 32:
        raise RuntimeError("MAILBOX_LOCAL_INSTANCE_SEED must contain at least 32 random characters.")
    local_instance_lock = threading.Lock()
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
        store.ensure_default_workspace()
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

    def local_keyless_registration_request(request):
        if not local_first_run or not local_first_owner_host(request):
            return False
        # Native local clients omit Origin; browsers must use the exact loopback origin.
        return not request.headers.get("origin") or local_first_owner_request(request)

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
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in {"/auth/login", "/auth/setup", "/auth/key-login", "/auth/register", "/auth/register-member", "/auth/signup"} and not request.headers.get("authorization"):
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
        ("/app.css", "app.css", "text/css"), ("/version.js", "version.js", "application/javascript"),
        ("/workspace.js", "workspace.js", "application/javascript"),
        ("/workspace", "workspace.html", "text/html"), ("/guide.js", "guide.js", "application/javascript"),
        ("/dashboard/chat", "chat.html", "text/html"), ("/chat.js", "chat.js", "application/javascript"),
        ("/chat.css", "chat.css", "text/css"),
        ("/join-workspace.js", "join-workspace.js", "application/javascript"),
        ("/join-workspace", "join-workspace.html", "text/html"),
        ("/join-member.js", "join-member.js", "application/javascript"),
        ("/join-member", "join-member.html", "text/html"),
        ("/favicon.svg", "favicon.svg", "image/svg+xml"),
        ("/brand-symbol.svg", "assets/brand-symbol.svg", "image/svg+xml"),
        ("/social-card.png", "assets/social-card.png", "image/png"),
        ("/use-cases", "use-cases.html", "text/html"), ("/use-cases.js", "use-cases.js", "application/javascript"),
        ("/mailbox-client.py", "mailbox_client.py", "text/x-python"),
        ("/agent-instructions.md", "docs/AGENT-INSTRUCTIONS.md", "text/plain"),
        ("/agent-bank.md", "docs/AGENT-MAILBOX-MASTER-PROMPT.md", "text/plain"),
        ("/agent-mailbox.md", "agent-mailbox.md", "text/plain"),
    ]:
        app.add_api_route(url, public_file(filename, mime), methods=["GET"], include_in_schema=False)

    @app.get("/onboarding")
    def onboarding_manifest():
        return JSONResponse(onboarding_catalog(), headers={"Cache-Control": "no-store"})

    @app.get("/onboarding/download", include_in_schema=False)
    def onboarding_download():
        return Response(core_archive(), media_type="application/zip", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'attachment; filename="agent-mailbox-onboarding.zip"'})

    @app.get("/onboarding/files/{resource_id}", include_in_schema=False)
    def onboarding_resource(resource_id: str):
        path = resource_file(resource_id)
        if path is None:
            raise HTTPException(404, "Onboarding resource not found")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/onboarding/resources/{resource_id}")
    def onboarding_resource_content(resource_id: str):
        result = resource_content(resource_id)
        if result is None:
            raise HTTPException(404, "Onboarding resource not found")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

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
            if path.startswith("/onboarding/resources/") and method == "GET":
                result = resource_content(path.rsplit("/", 1)[1])
                if result is None:
                    raise HTTPException(404, "Onboarding resource not found")
                return result
            if path == "/inbox/summary" and method == "GET":
                return inbox_summary_for(identifier)
            if path == "/outbox" and method == "GET":
                return page_mail(identifier, "outbox", query)
            if path == "/profiles" and method == "GET":
                workspace_id = store.workspace_for_agent(identifier)
                return profiles_directory({workspace_id, *store.connected_workspaces(workspace_id)})
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
                workspace_id = store.workspace_for_agent(identifier)
                return profile_instances(path.split("/")[2], {workspace_id, *store.connected_workspaces(workspace_id)})
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
                workspace_id = store.workspace_for_agent(identifier)
                workspace_ids = {workspace_id, *store.connected_workspaces(workspace_id)}
                profiles = {row["id"]: row for row in store.profiles_all()}
                agents = [public_agent(row, now, profiles.get(row.get("profile_id"))) for row in store.all_agents()
                          if (row.get("workspace_id") or DEFAULT_WORKSPACE_ID) in workspace_ids]
                if query.get("online") in ("true", "false"):
                    agents = [row for row in agents if row["online"] == (query["online"] == "true")]
                limit, offset = int(query.get("limit", 100)), int(query.get("offset", 0))
                if not 1 <= limit <= 200 or offset < 0:
                    raise HTTPException(422, "Invalid directory page")
                return {"agents": agents[offset:offset + limit], "total": len(agents), "limit": limit, "offset": offset}
            if path == "/agents/discover" and method == "GET":
                workspace_id = store.workspace_for_agent(identifier)
                return discover_agents(query, {workspace_id, *store.connected_workspaces(workspace_id)})
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
                attachments = prepare_attachments(message.attachments, attachment_limit)
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
                              "workspace_id": session.get("workspace_id") or DEFAULT_WORKSPACE_ID,
                              "workspace_scoped": session.get("session_type") == "account",
                              "account_session_id": session["id"], "csrf_hash": session["csrf_hash"],
                              "auth_method": session["auth_method"]}
                    return ({"admin": True} | common if common["role"] == "owner" else
                            {"admin": False, "control_user": True} | common)
            raise HTTPException(401, "Account session required", headers={"WWW-Authenticate": "Bearer"})
        token = credentials.credentials
        if secrets.compare_digest(token.encode(), hub_key.encode()):
            return {"admin": True}
        credential_hash = hashlib.sha256(token.encode()).hexdigest()
        identifier = store.authenticate(credential_hash)
        if identifier is not None:
            return {"admin": False, "id": identifier, "credential_type": "instance", "credential_hash": credential_hash}
        api_key = store.authenticate_agent_api_key(credential_hash)
        if api_key is not None:
            return {"admin": False, "id": api_key["id"], "credential_type": "api_key",
                    "api_key_id": api_key["api_key_id"]}
        registration = store.account_by_registration_key_hash(credential_hash)
        if registration is not None:
            return {"admin": False, "control_user": True, "registration_key": True,
                    "id": registration["user_id"], "username": registration["username"],
                    "name": registration["username"], "role": registration["role"],
                    "workspace_id": registration["workspace_id"], "workspace_scoped": True}
        if identifier is None:
            user = store.control_user_by_hash(hashlib.sha256(token.encode()).hexdigest())
            if user:
                return {"admin": False, "control_user": True, "id": user["id"],
                        "name": user["name"], "role": user["role"]}
            raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
        return {"admin": False, "id": identifier, "credential_type": "instance"}

    def create_agent_api_key(identifier, name, expected_instance_hash):
        token = "amb_live_" + secrets.token_urlsafe(32)
        row = {"id": str(uuid4()), "agent_id": identifier, "name": name,
               "key_prefix": token[:17], "token_hash": hashlib.sha256(token.encode()).hexdigest(),
               "created_at": time.time(), "last_used_at": None, "revoked_at": None}
        store.create_agent_api_key(row, expected_instance_hash)
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
        response = JSONResponse({"user": {"id": user["id"], "username": user["username"], "role": user.get("role", "owner"),
                                           "workspace_id": user.get("workspace_id") or DEFAULT_WORKSPACE_ID},
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
                "last_login_at": now, "disabled_at": None, "workspace_id": DEFAULT_WORKSPACE_ID,
                "display_name": body.display_name, "organization": body.organization,
                "project": body.project, "description": body.description}
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

    @app.post("/auth/register", status_code=201)
    def register_workspace_account(body: WorkspaceSignup, request: Request):
        username = body.username.strip()
        rate_key = account_login_rate_key(username.casefold(), request)
        if not store.account_login_allowed(rate_key, time.time()):
            raise HTTPException(429, "Too many account attempts. Retry in 15 minutes.",
                                headers={"Retry-After": "900"})
        now = time.time()
        user = {"id": str(uuid4()), "username": username, "username_norm": username.casefold(),
                "password_hash": request_password_hash(body.password), "role": "workspace_owner",
                "created_at": now, "last_login_at": now, "disabled_at": None,
                "display_name": body.display_name, "organization": body.organization,
                "project": body.project, "description": body.description}
        workspace = {"id": str(uuid4()), "name": body.workspace_name.strip(), "created_at": now}
        try:
            account = store.accept_workspace_invite(
                hashlib.sha256(body.invite_code.strip().encode()).hexdigest(), user, workspace)
        except HTTPException:
            store.record_account_login(rate_key, now, False)
            raise
        store.record_account_login(rate_key, now, True)
        store.record_control_event("workspace-invite", "workspace_owner_registered", account["id"])
        return establish_account_session(account, request, status_code=201)

    @app.post("/auth/register-member", status_code=201)
    def register_workspace_member(body: MemberSignup, request: Request):
        username = body.username.strip()
        rate_key = account_login_rate_key(username.casefold(), request)
        if not store.account_login_allowed(rate_key, time.time()):
            raise HTTPException(429, "Too many account attempts. Retry in 15 minutes.", headers={"Retry-After": "900"})
        now = time.time()
        user = {"id": str(uuid4()), "username": username, "username_norm": username.casefold(),
                "password_hash": request_password_hash(body.password), "role": "workspace_member",
                "created_at": now, "last_login_at": now, "disabled_at": None,
                "display_name": body.display_name, "organization": body.organization,
                "project": body.project, "description": body.description}
        workspace = {"id": str(uuid4()), "name": "", "created_at": now}
        try:
            account = store.accept_workspace_invite(hashlib.sha256(body.invite_code.strip().encode()).hexdigest(), user, workspace)
        except HTTPException:
            store.record_account_login(rate_key, now, False)
            raise
        store.record_account_login(rate_key, now, True)
        store.record_control_event("workspace-member-invite", "workspace_member_registered", account["id"])
        return establish_account_session(account, request, status_code=201)

    @app.post("/auth/signup", status_code=201)
    def signup_workspace_account(body: AccountSignup, request: Request):
        if not store.owner_account_exists():
            raise HTTPException(409, "The server owner must complete first-time setup before new accounts can sign up")
        username = body.username.strip()
        rate_key = account_login_rate_key(username.casefold(), request)
        if not store.account_login_allowed(rate_key, time.time()):
            raise HTTPException(429, "Too many account attempts. Retry in 15 minutes.",
                                headers={"Retry-After": "900"})
        now = time.time()
        workspace = {"id": str(uuid4()), "name": body.workspace_name.strip(), "created_at": now}
        user = {"id": str(uuid4()), "username": username, "username_norm": username.casefold(),
                "password_hash": request_password_hash(body.password), "role": "workspace_owner",
                "created_at": now, "last_login_at": now, "disabled_at": None,
                "workspace_id": workspace["id"], "display_name": body.display_name,
                "organization": body.organization, "project": body.project,
                "description": body.description}
        try:
            account = store.create_workspace_account(user, workspace)
        except HTTPException:
            store.record_account_login(rate_key, now, False)
            raise
        store.record_account_login(rate_key, now, True)
        store.record_control_event("self-signup", "workspace_account_registered", account["id"])
        return establish_account_session(account, request, status_code=201)

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
                                       "username": session["username"], "role": session.get("role", "owner"),
                                       "workspace_id": session.get("workspace_id") or DEFAULT_WORKSPACE_ID},
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
        if account and account.get("role") == "workspace_owner":
            raise HTTPException(403, "The server owner cannot reset an independent workspace owner's password")
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

    @app.get("/account/profile")
    def account_profile(request: Request):
        session = current_account_session(request)
        account = store.account_by_id(session["user_id"])
        if not account:
            raise HTTPException(404, "Account not found")
        workspace_id = account.get("workspace_id") or DEFAULT_WORKSPACE_ID
        registration = store.account_registration_key(account["id"])
        return JSONResponse({"account": {key: account.get(key) for key in ("id", "username", "role", "created_at", "last_login_at", "workspace_id", "display_name", "organization", "project", "description")},
                             "workspace": store.workspace_info(workspace_id),
                             "registration_key": public_registration_key(registration) if registration else None,
                             "registration": {"enabled": account.get("role") in ("owner", "workspace_owner", "operator", "workspace_member"),
                                               "endpoint": "/join/secure", "header": "Authorization: Bearer <registration_key>",
                                               "scope": "Only agents registered with this key enter this account's workspace."}},
                            headers={"Cache-Control": "no-store"})

    @app.patch("/account/profile")
    def update_account_profile(body: AccountProfilePatch, request: Request):
        session = current_account_session(request)
        account = store.account_by_id(session["user_id"])
        if not account:
            raise HTTPException(404, "Account not found")
        fields = {key: value or "" for key, value in body.model_dump(exclude_unset=True).items()}
        updated = store.update_account_profile(account["id"], fields)
        store.record_control_event(account["id"], "account_profile_updated", account["id"])
        return {"account": {key: updated.get(key) for key in ("id", "username", "role", "created_at", "last_login_at", "workspace_id", "display_name", "organization", "project", "description")}}

    @app.get("/account/members")
    def account_members(request: Request):
        session = current_account_session(request)
        account = store.account_by_id(session["user_id"])
        if not account:
            raise HTTPException(404, "Account not found")
        workspace_id = account.get("workspace_id") or DEFAULT_WORKSPACE_ID
        members = []
        for row in store.all_account_users(workspace_id):
            members.append({key: row.get(key) for key in
                            ("id", "username", "role", "created_at", "last_login_at", "disabled_at", "workspace_id")})
        return JSONResponse({"workspace_id": workspace_id, "members": members,
                             "count": len(members), "current_user_id": account["id"]},
                            headers={"Cache-Control": "no-store"})

    @app.post("/account/registration-key")
    def rotate_account_registration_key(request: Request):
        session = current_account_session(request)
        account = store.account_by_id(session["user_id"])
        if not account:
            raise HTTPException(404, "Account not found")
        if account.get("role") not in ("owner", "workspace_owner", "operator", "workspace_member"):
            raise HTTPException(403, "This account cannot register agents")
        token = "amr_" + secrets.token_urlsafe(32)
        row = store.create_or_rotate_account_registration_key(
            account["id"], account.get("workspace_id") or DEFAULT_WORKSPACE_ID,
            hashlib.sha256(token.encode()).hexdigest(), token[:16], time.time())
        store.record_control_event(account["id"], "account_registration_key_rotated", account["id"])
        return JSONResponse({"registration_key": token, "metadata": public_registration_key(row),
                             "warning": "Copy this key now. It is never shown again; rotating it revokes the previous key."},
                            headers={"Cache-Control": "no-store"})

    @app.delete("/account/registration-key")
    def revoke_account_registration_key(request: Request):
        session = current_account_session(request)
        account = store.account_by_id(session["user_id"])
        if not account:
            raise HTTPException(404, "Account not found")
        if account.get("role") not in ("owner", "workspace_owner", "operator", "workspace_member"):
            raise HTTPException(403, "This account cannot manage agent registration")
        revoked = store.revoke_account_registration_key(account["id"], time.time())
        if revoked:
            store.record_control_event(account["id"], "account_registration_key_revoked", account["id"])
        return {"revoked": revoked}

    def agent_id(identity):
        if identity["admin"] or identity.get("control_user"):
            raise HTTPException(403, "Use an agent token for messaging and presence")
        return identity["id"]

    def can_manage_agents(identity):
        return identity.get("admin", False) or (identity.get("control_user") and identity.get("role") in ("operator", "workspace_owner", "workspace_member"))

    def identity_workspace(identity):
        if identity.get("workspace_id"):
            return identity["workspace_id"]
        if identity.get("id") and not identity.get("admin") and not identity.get("control_user"):
            return store.workspace_for_agent(identity["id"])
        return DEFAULT_WORKSPACE_ID

    def workspace_registration_seed(workspace_id):
        return hmac.new(hub_key.encode(), ("agent-mailbox-workspace:" + (workspace_id or DEFAULT_WORKSPACE_ID)).encode(), hashlib.sha256).hexdigest()

    def public_registration_key(row):
        return {key: row.get(key) for key in ("user_id", "workspace_id", "key_prefix", "created_at", "rotated_at", "revoked_at")}

    def visible_workspace_ids(identity):
        workspace_id = identity_workspace(identity)
        return {workspace_id, *store.connected_workspaces(workspace_id)}

    def can_manage_workspace(identity):
        return identity.get("admin", False) or (
            identity.get("workspace_scoped") and identity.get("role") == "workspace_owner")

    def require_workspace_manager(identity):
        if not can_manage_workspace(identity):
            raise HTTPException(403, "Workspace invitations require a workspace owner account")

    def require_manage_own_agent(identity, identifier):
        if not can_manage_agents(identity):
            raise HTTPException(403, "Agent management requires a workspace account")
        if store.workspace_for_agent(identifier) != identity_workspace(identity):
            raise HTTPException(404, "Agent not found in your workspace")
        if identity.get("role") == "workspace_member":
            agent = store.get_agent(identifier)
            if not agent or agent.get("created_by_account_id") != identity.get("id"):
                raise HTTPException(403, "This member can manage only agents registered by their account")

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
                      chat_path=row.get("chat_path") or "",
                      description=row.get("description") or profile.get("description", ""),
                      capabilities=row.get("capabilities") or profile.get("capabilities", []))
        result["workspace_id"] = row.get("workspace_id") or profile.get("workspace_id") or DEFAULT_WORKSPACE_ID
        result["added_by"] = {"account_id": row.get("created_by_account_id") or profile.get("created_by_account_id"),
                               "username": row.get("created_by_username") or profile.get("created_by_username") or "Sistema / registro directo",
                               "method": row.get("registration_method") or profile.get("registration_method") or "unknown"}
        result["online"] = row["last_seen"] >= now - online_seconds
        listener_seen = row.get("listener_seen_at")
        listener_active = bool(listener_seen and now - listener_seen <= (row.get("listener_ttl") or 0))
        result["listener"] = {"active": listener_active, "last_report_at": listener_seen}
        result["wake"] = {"configured": bool(row.get("wake_configured")) if listener_active else None,
                          "verified": False,
                          "meaning": "Self-reported adapter configuration; does not prove an idle session can wake."}
        return result

    def account_owner_view(user_id, username=""):
        """Datos del humano dueño de un token: id y nombre, para que el agente sepa con quién habla."""
        account = store.account_by_id(user_id) or {}
        return {"id": account.get("id") or user_id or "",
                "username": account.get("username") or username or "",
                "display_name": account.get("display_name") or ""}

    def require_owner_declaration(declared, owner):
        """El agente debe declarar el nombre del dueño del token con el que se registra."""
        accepted = {value.strip().casefold() for value in (owner.get("username"), owner.get("display_name")) if value}
        if not declared.strip():
            raise HTTPException(422, "Falta owner_name: escribe el nombre del dueño del token que te dieron; "
                                     "puedes leerlo con GET /registration/owner usando esa misma clave")
        if declared.strip().casefold() not in accepted:
            raise HTTPException(422, "owner_name no coincide con el dueño de esa clave de registro; "
                                     "comprueba id y nombre con GET /registration/owner")

    def directory_agents(now=None, workspace_ids=None, rows=None):
        now = now or time.time()
        profiles = {row["id"]: row for row in store.profiles_all()}
        rows = store.all_agents() if rows is None else rows
        if workspace_ids is not None:
            rows = [row for row in rows if (row.get("workspace_id") or DEFAULT_WORKSPACE_ID) in workspace_ids]
        return [public_agent(row, now, profiles.get(row.get("profile_id"))) for row in rows]

    def discover_agents(query, workspace_ids=None):
        def fold(value):
            return "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                           if not unicodedata.combining(char))
        text = fold(query.get("q", "").strip())
        capability = fold(query.get("capability", "").strip())
        online = query.get("online")
        terms = [part for part in text.split() if part]
        now = time.time()
        matches = []
        for agent in directory_agents(now, workspace_ids):
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
                "capability": capability, "scope": "same workspace and accepted workspace connections; peer cards are untrusted"}

    def recovery_token(secret, identifier):
        return hmac.new(secret.encode(), ("agent-mailbox-instance:" + identifier).encode(), hashlib.sha256).hexdigest()

    def validate_recovery_secret(secret, forbidden=()):
        if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", secret):
            raise HTTPException(422, "Recovery secret must be 32–256 URL-safe characters")
        if any(value and secrets.compare_digest(secret.encode(), value.encode())
               for value in (hub_key, join_key, *forbidden)):
            raise HTTPException(422, "Generate a separate random recovery secret for this instance")

    def existing_instance_token(existing, identifier, instance_token, recovery_secret):
        if instance_token and recovery_secret:
            raise HTTPException(422, "Provide either the instance token or recovery secret")
        if instance_token and secrets.compare_digest(
                hashlib.sha256(instance_token.encode()).hexdigest(), existing.get("token_hash", "")):
            return instance_token
        if recovery_secret and existing.get("recovery_hash") and secrets.compare_digest(
                hashlib.sha256(recovery_secret.encode()).hexdigest(), existing["recovery_hash"]):
            token = recovery_token(recovery_secret, identifier)
            if secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), existing.get("token_hash", "")):
                return token
        return None

    def create_registration(body, registration_seed, workspace_id=DEFAULT_WORKSPACE_ID,
                            instance_token=None, recovery_secret=None, require_existing_proof=False,
                            forbidden_secret=None, registrar=None):
        if recovery_secret:
            validate_recovery_secret(recovery_secret, (forbidden_secret,))
        session_key = body.session_key or secrets.token_urlsafe(24)
        namespace = uuid5(NAMESPACE_URL, "agent-mailbox-registration:" +
                          hashlib.sha256(registration_seed.encode()).hexdigest())
        identifier = str(uuid5(namespace, session_key))
        derived_token = hmac.new(registration_seed.encode(), identifier.encode(), hashlib.sha256).hexdigest()
        with local_instance_lock:
            existing = store.get_agent(identifier)
            if existing:
                token = (existing_instance_token(existing, identifier, instance_token, recovery_secret)
                         if require_existing_proof else derived_token)
                token_hash = hashlib.sha256((token or "").encode()).hexdigest()
                if (not token or not secrets.compare_digest(existing.get("token_hash", ""), token_hash) or
                        (existing.get("workspace_id") or DEFAULT_WORKSPACE_ID) != workspace_id):
                    raise HTTPException(409, "Session ID is already bound to another credential.")
                agent = public_agent(existing, time.time())
                return {"agent": agent, "token": token, "session_id": session_key,
                        "resumed": True, "onboarding": onboarding_handoff(agent, session_key)}
            now = time.time()
            if instance_token:
                raise HTTPException(422, "An instance token can only resume an existing session")
            token = recovery_token(recovery_secret, identifier) if recovery_secret else derived_token
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            row = dict(id=identifier, name=body.name, kind=body.kind, project=body.project,
                       chat_path=body.chat_path,
                       description=body.description, capabilities=body.capabilities,
                       token_hash=token_hash, created_at=now, last_seen=now, workspace_id=workspace_id,
                       recovery_hash=hashlib.sha256(recovery_secret.encode()).hexdigest() if recovery_secret else None,
                       created_by_account_id=(registrar or {}).get("account_id"),
                       created_by_username=(registrar or {}).get("username", ""),
                       registration_method=(registrar or {}).get("method", "join_key"))
            if store.register(row) is False:
                existing = store.get_agent(identifier)
                token = (existing_instance_token(existing, identifier, instance_token, recovery_secret)
                         if existing and require_existing_proof else derived_token)
                if (not existing or not token or
                        not secrets.compare_digest(existing.get("token_hash", ""), hashlib.sha256(token.encode()).hexdigest()) or
                        (existing.get("workspace_id") or DEFAULT_WORKSPACE_ID) != workspace_id):
                    raise HTTPException(409, "Session ID is already bound to another credential.")
                agent = public_agent(existing, time.time())
                return {"agent": agent, "token": token, "session_id": session_key,
                        "resumed": True, "onboarding": onboarding_handoff(agent, session_key)}
        agent = public_agent(row, now)
        return {"agent": agent, "token": token, "session_id": session_key, "resumed": False,
                "onboarding": onboarding_handoff(agent, session_key)}

    def public_profile(row):
        result = {key: row.get(key, [] if key == "capabilities" else "") for key in
                  ("id", "name", "kind", "project", "created_at", "description", "capabilities")}
        result["workspace_id"] = row.get("workspace_id") or DEFAULT_WORKSPACE_ID
        result["added_by"] = {"account_id": row.get("created_by_account_id"),
                               "username": row.get("created_by_username") or "Sistema / registro directo",
                               "method": row.get("registration_method") or "unknown"}
        return result

    def profiles_directory(workspace_ids=None):
        agents = store.all_agents()
        if workspace_ids is not None:
            agents = [a for a in agents if (a.get("workspace_id") or DEFAULT_WORKSPACE_ID) in workspace_ids]
        visible_profiles = {a.get("profile_id") for a in agents if a.get("profile_id")}
        return {"profiles": [{**public_profile(row),
                              "instances_count": sum(a.get("profile_id") == row["id"] for a in agents)}
                             for row in store.profiles_all() if row["id"] in visible_profiles]}

    def profile_instances(profile_id, workspace_ids=None):
        profile = store.profile_get(profile_id)
        if not profile:
            raise HTTPException(404, "Profile not found")
        members = [row for row in store.all_agents() if row.get("profile_id") == profile_id]
        if workspace_ids is not None:
            members = [row for row in members if (row.get("workspace_id") or DEFAULT_WORKSPACE_ID) in workspace_ids]
            if not members:
                raise HTTPException(404, "Profile not found")
        return {"profile_id": profile_id, "profile": public_profile(profile),
                "instances": [public_agent(row, time.time(), profile)
                for row in members]}

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
                   "created_at_iso", "message_type", "delivery_mode", "wake_backlog",
                   "attachment_count", "has_images", "has_files")} for row in page["messages"]]
        return {**result, "instance": public_agent(store.get_agent(identifier), time.time()),
                "preview": preview, "has_more": page["has_more"], "checked_at": time.time()}

    def workflow_bundle():
        root = FilePath(__file__).parent
        return {"version": APP_VERSION, "skill": (root / "skills/agent-mailbox/SKILL.md").read_text(encoding="utf-8"),
                "master_prompt": (root / "docs/AGENT-MAILBOX-MASTER-PROMPT.md").read_text(encoding="utf-8"),
                "persistent_instructions": (root / "docs/AGENT-INSTRUCTIONS.md").read_text(encoding="utf-8"),
                "entrypoint": (root / "agent-mailbox.md").read_text(encoding="utf-8"),
                "onboarding": onboarding_catalog(),
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
        attachments = prepare_attachments(message.attachments, attachment_limit)
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
        workspace_id = store.workspace_for_agent(sender)
        workspace_ids = {workspace_id, *store.connected_workspaces(workspace_id)}
        recipients = [row for row in store.all_agents()
                      if row["id"] != sender
                      and (row.get("workspace_id") or DEFAULT_WORKSPACE_ID) in workspace_ids
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

    @app.get("/dashboard/account", include_in_schema=False)
    def dashboard_account_page():
        return FileResponse(FilePath(__file__).parent / "account.html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        })

    @app.get("/account.js", include_in_schema=False)
    def dashboard_account_script():
        return FileResponse(FilePath(__file__).parent / "account.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/account-members.js", include_in_schema=False)
    def dashboard_account_members_script():
        return FileResponse(FilePath(__file__).parent / "account-members.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/account-edit.js", include_in_schema=False)
    def dashboard_account_edit_script():
        return FileResponse(FilePath(__file__).parent / "account-edit.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/dashboard/profiles", include_in_schema=False)
    def dashboard_profiles_page():
        return FileResponse(FilePath(__file__).parent / "profiles.html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        })

    @app.get("/profiles.js", include_in_schema=False)
    def dashboard_profiles_script():
        return FileResponse(FilePath(__file__).parent / "profiles.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/dashboard/profile/{profile_id}", include_in_schema=False)
    def dashboard_profile_page(profile_id: str):
        return FileResponse(FilePath(__file__).parent / "profile.html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        })

    @app.get("/profile.js", include_in_schema=False)
    def dashboard_profile_script():
        return FileResponse(FilePath(__file__).parent / "profile.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/profile-edit.js", include_in_schema=False)
    def dashboard_profile_edit_script():
        return FileResponse(FilePath(__file__).parent / "profile-edit.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/dashboard/message/{message_id}", include_in_schema=False)
    def dashboard_message_page(message_id: int):
        return FileResponse(FilePath(__file__).parent / "message.html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        })

    @app.get("/message.js", include_in_schema=False)
    def dashboard_message_script():
        return FileResponse(FilePath(__file__).parent / "message.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    def message_wake_state(row, replies):
        """What was asked and what the server can actually prove. Never inflate it."""
        if row.get("delivery_mode") != "wake":
            return {"mode": "inbox", "state": "sin_despertar", "evidence": None, "at": None,
                    "label": "Mensaje normal: no pedía despertar a nadie"}
        if replies:
            return {"mode": "wake", "state": "atendido", "evidence": "respondido",
                    "at": replies[-1].get("created_at"),
                    "label": "Wake atendido: el destinatario respondió"}
        if row.get("acknowledged_at"):
            return {"mode": "wake", "state": "atendido", "evidence": "acuse", "at": row["acknowledged_at"],
                    "label": "Wake atendido: el destinatario confirmó el mensaje"}
        return {"mode": "wake", "state": "solicitado", "evidence": None, "at": None,
                "label": "Wake solicitado: todavía sin evidencia de que el turno se abriera"}

    def message_thread_entry(row):
        return {key: row.get(key) for key in
                ("id", "thread_id", "in_reply_to", "from_agent_id", "to_agent_id", "message_type",
                 "delivery_mode", "wake_backlog", "status", "subject", "created_at", "acknowledged_at",
                 "attachment_count")}

    @app.get("/dashboard/messages/{message_id}")
    def dashboard_message(message_id: int, identity: Identity):
        require_dashboard_identity(identity)
        workspace_id = identity_workspace(identity)
        row = store.message_for_workspace(message_id, workspace_id)
        if not row:
            raise HTTPException(404, "Message not found in your workspace")
        now = time.time()
        can_read_content = bool(identity["admin"] or identity.get("role") in ("operator", "workspace_owner"))
        visible = row if can_read_content else {
            key: row.get(key) for key in
            ("id", "thread_id", "in_reply_to", "from_agent_id", "to_agent_id", "message_type", "delivery_mode",
             "wake_backlog", "status", "created_at", "acknowledged_at", "attachment_count", "has_images",
             "has_files", "request_fingerprint")}
        thread = store.dashboard_thread(row.get("thread_id") or message_id)
        replies = [entry for entry in thread if entry.get("in_reply_to") == message_id]
        participants = {}
        for role, key in (("from", "from_agent_id"), ("to", "to_agent_id")):
            agent = store.get_agent(row.get(key))
            participants[role] = public_agent(agent, now) if agent else None
        return {"message": visible, "wake": message_wake_state(row, replies), "participants": participants,
                "thread": [message_thread_entry(entry) for entry in thread], "thread_count": len(thread),
                "replies": [message_thread_entry(entry) for entry in replies],
                "content_visible": can_read_content, "checked_at": now}

    @app.get("/dashboard/data")
    def dashboard_data(identity: Identity):
        if not identity["admin"] and not identity.get("control_user"):
            raise HTTPException(403, "El panel requiere una clave de administración u operador")
        now = time.time()
        workspace_id = identity_workspace(identity)
        workspace_ids = visible_workspace_ids(identity)
        all_agents = store.all_agents()
        if identity.get("role") == "workspace_member":
            all_agents = [row for row in all_agents if row.get("created_by_account_id") == identity.get("id")]
        agents_by_id = {row["id"]: row for row in all_agents}
        agents = directory_agents(now, workspace_ids, all_agents)
        agents = [{**row, "can_manage": (agents_by_id[row["id"]].get("workspace_id") or DEFAULT_WORKSPACE_ID) == workspace_id,
                   "credential_epoch": agents_by_id[row["id"]].get("credential_epoch", 0)
                       if (agents_by_id[row["id"]].get("workspace_id") or DEFAULT_WORKSPACE_ID) == workspace_id else None}
                  for row in agents]
        own_agent_ids = [row["id"] for row in all_agents if (row.get("workspace_id") or DEFAULT_WORKSPACE_ID) == workspace_id]
        messages = store.recent_messages(15, own_agent_ids)
        if identity.get("role") == "observer":
            messages = [{key: row.get(key) for key in
                         ("id", "from_agent_id", "to_agent_id", "created_at", "acknowledged_at",
                          "thread_id", "status", "message_type", "attachment_count", "has_images", "has_files")}
                        for row in messages]
        permissions = {"is_admin": identity["admin"], "role": "owner" if identity["admin"] else identity.get("role"),
                       "can_manage_agents": can_manage_agents(identity),
                       "can_manage_workspace": can_manage_workspace(identity),
                       "can_manage_users": identity["admin"], "can_read_message_content": identity["admin"] or identity.get("role") == "operator",
                       "can_delete_messages": identity["admin"] or (identity.get("workspace_scoped") and identity.get("role") == "workspace_owner")}
        permissions["can_read_message_content"] = permissions["can_read_message_content"] or identity.get("role") == "workspace_owner"
        return {"stats": {**store.workspace_stats(workspace_id, now - online_seconds, all_agents), "online_window_seconds": online_seconds},
                "agents": agents, "messages": messages, "permissions": permissions, "updated_at": now}

    def require_dashboard_identity(identity):
        if identity.get("registration_key"):
            raise HTTPException(403, "Registration keys are for agent enrollment only")
        if not identity["admin"] and not identity.get("control_user"):
            raise HTTPException(403, "The dashboard requires an account session")

    def human_chat_identity(identity, create=False):
        # A separate, server-owned mailbox for each account, never an agent's token.
        if not identity.get("workspace_scoped") or not identity.get("account_session_id"):
            raise HTTPException(403, "Sign in with your own account to use chat")
        account = store.account_by_id(identity["id"])
        if not account or account.get("disabled_at"):
            raise HTTPException(403, "Account unavailable")
        if account.get("role") not in ("owner", "workspace_owner", "workspace_member", "operator"):
            raise HTTPException(403, "This account cannot use chat")
        workspace_id = identity_workspace(identity)
        identifier = str(uuid5(NAMESPACE_URL, f"agent-mailbox:human:{workspace_id}:{account['id']}"))
        if create and not store.get_agent(identifier):
            now = time.time()
            store.register(dict(id=identifier, name=account.get("display_name") or account["username"],
                                kind="human", project="Dashboard chat", token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                                created_at=now, last_seen=0, workspace_id=workspace_id,
                                description="Usuario humano del workspace", capabilities=[],
                                created_by_account_id=account["id"], created_by_username=account["username"],
                                registration_method="account_chat"))
        return identifier, account

    @app.get("/dashboard/chat/data")
    def dashboard_chat_data(identity: Identity, after_id: int = Query(0, ge=0, le=MAX_MESSAGE_ID)):
        identifier, account = human_chat_identity(identity)
        directory = [row for row in directory_agents(workspace_ids=visible_workspace_ids(identity))
                     if row["kind"] != "human"]
        if not store.get_agent(identifier):
            return {"agents": directory, "messages": [], "next_after_id": after_id, "has_more": False}
        inbox = store.mail_page(identifier, "inbox", after_id=after_id, limit=100)
        outbox = store.mail_page(identifier, "outbox", after_id=after_id, limit=100)
        rows = sorted(inbox["messages"] + outbox["messages"], key=lambda row: row["id"])
        page = rows[:100]
        return {"agents": directory, "messages": page, "next_after_id": page[-1]["id"] if page else after_id,
                "has_more": len(rows) > 100 or inbox["has_more"] or outbox["has_more"],
                "human_instance_id": identifier}

    @app.post("/dashboard/chat/messages", status_code=201)
    def dashboard_chat_send(body: Message, identity: Identity):
        if body.in_reply_to is not None or body.message_type != "request":
            raise HTTPException(422, "Send a new request to the selected instance")
        if body.wake_backlog and body.delivery_mode != "wake":
            raise HTTPException(422, "wake_backlog requires delivery_mode='wake'")
        identifier, account = human_chat_identity(identity, create=True)
        recipient = store.get_agent(body.to_agent_id)
        if not recipient or recipient.get("kind") == "human" or (recipient.get("workspace_id") or DEFAULT_WORKSPACE_ID) not in visible_workspace_ids(identity):
            raise HTTPException(404, "Agent instance not found")
        workspace_id = identity_workspace(identity)
        workspace = store.workspace_info(workspace_id) or {}
        sender = {"username": account["username"], "name": account.get("display_name") or account["username"],
                  "account_id": account["id"], "role": account.get("role", "member"),
                  "workspace_id": workspace_id, "workspace_name": workspace.get("name") or workspace_id}
        attachments = prepare_attachments(body.attachments, attachment_limit)
        if not body.text.strip() and not attachments:
            raise HTTPException(422, "Message text cannot be blank")
        # This header is produced by the server, not accepted as a client claim.
        prefix = "Mensaje del usuario autenticado: " + json.dumps(sender, ensure_ascii=False) + "\n\n"
        if len(prefix) + len(body.text) > 32768:
            raise HTTPException(422, "Message is too long")
        message = body.model_copy(update={"text": prefix + body.text})
        return store.send(identifier, message, attachments)

    @app.get("/dashboard/chat/attachments/{message_id}/{attachment_index}")
    def dashboard_chat_attachment(message_id: int, attachment_index: int, identity: Identity):
        identifier, _ = human_chat_identity(identity)
        attachment = store.get_attachment(identifier, message_id, attachment_index)
        return JSONResponse({"filename": attachment["filename"], "media_type": attachment["media_type"],
                             "data_base64": base64.b64encode(attachment["data"]).decode("ascii")},
                            headers={"Cache-Control": "no-store"})

    def dashboard_profile_access(profile_id, identity):
        require_dashboard_identity(identity)
        workspace_id = identity_workspace(identity)
        profile = store.profile_get(profile_id)
        if not profile or (profile.get("workspace_id") or DEFAULT_WORKSPACE_ID) != workspace_id:
            raise HTTPException(404, "Profile not found in your workspace")
        if identity.get("role") == "workspace_member" and profile.get("created_by_account_id") != identity.get("id"):
            raise HTTPException(403, "This member can view only profiles registered by their account")
        return profile, workspace_id

    @app.get("/dashboard/profiles/data")
    def dashboard_profiles(identity: Identity):
        require_dashboard_identity(identity)
        workspace_id = identity_workspace(identity)
        agents = [row for row in store.all_agents() if (row.get("workspace_id") or DEFAULT_WORKSPACE_ID) == workspace_id]
        if identity.get("role") == "workspace_member":
            agents = [row for row in agents if row.get("created_by_account_id") == identity.get("id")]
        profile_ids = {row.get("profile_id") for row in agents if row.get("profile_id")}
        now = time.time()
        result = []
        for profile in store.profiles_all():
            if profile["id"] not in profile_ids or (profile.get("workspace_id") or DEFAULT_WORKSPACE_ID) != workspace_id:
                continue
            members = [row for row in agents if row.get("profile_id") == profile["id"]]
            result.append({**public_profile(profile), "instances_count": len(members),
                           "online_count": sum(row.get("last_seen", 0) >= now - online_seconds for row in members),
                           "route": f"/dashboard/profile/{profile['id']}"})
        return {"profiles": sorted(result, key=lambda row: row.get("name", "").casefold())}

    @app.get("/dashboard/profiles/{profile_id}")
    def dashboard_profile_detail(profile_id: str, identity: Identity):
        profile, workspace_id = dashboard_profile_access(profile_id, identity)
        now = time.time()
        instances = []
        for row in store.all_agents():
            if row.get("profile_id") != profile_id or (row.get("workspace_id") or DEFAULT_WORKSPACE_ID) != workspace_id:
                continue
            instances.append({**public_agent(row, now, profile),
                              "credential_epoch": row.get("credential_epoch", 0),
                              "has_recovery_secret": bool(row.get("recovery_hash")),
                              "rotated_at": row.get("credential_rotated_at"),
                              "token_visibility": "never-readable; rotate to issue a new one"})
        return {"profile": public_profile(profile), "instances": instances,
                "security": {"raw_tokens": False, "enrollment_token": False,
                              "instance_tokens": "Only returned at registration or explicit rotation",
                              "route": f"/dashboard/profile/{profile_id}"}}

    @app.patch("/dashboard/profiles/{profile_id}")
    def rename_dashboard_profile(profile_id: str, body: AgentRename, identity: Identity):
        profile, _ = dashboard_profile_access(profile_id, identity)
        updated = store.update_profile_card(profile_id, {"name": body.name})
        return public_profile(updated)

    @app.post("/dashboard/agents/{identifier}/create-profile", status_code=201)
    def create_dashboard_profile(identifier: str, body: AgentRename, identity: Identity):
        require_dashboard_identity(identity)
        require_manage_own_agent(identity, identifier)
        agent = store.get_agent(identifier)
        if not agent or agent.get("kind") == "human":
            raise HTTPException(404, "Agent not found")
        profile, enrollment_token = store.create_profile(identifier, body.name, agent["kind"], agent.get("project", ""))
        return JSONResponse({"profile": public_profile(profile),
                             "instance": public_agent(store.get_agent(identifier), time.time(), profile),
                             "enrollment_token": enrollment_token,
                             "notice": "Save this enrollment token now; it cannot be shown again."},
                            status_code=201, headers={"Cache-Control": "no-store"})

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
        return {"accounts": store.all_account_users(identity_workspace(identity))}

    @app.post("/dashboard/accounts", status_code=201)
    def create_dashboard_account(body: TeamAccountCreate, identity: Identity):
        require_admin(identity)
        active = [row for row in store.all_account_users(identity_workspace(identity)) if row.get("disabled_at") is None]
        if len(active) >= 100:
            raise HTTPException(409, "Account limit reached; disable an unused account first")
        username = body.username.strip()
        row = {"id": str(uuid4()), "username": username, "username_norm": username.casefold(),
               "password_hash": request_password_hash(body.password), "role": body.role, "created_at": time.time(),
               "last_login_at": None, "disabled_at": None, "workspace_id": identity_workspace(identity)}
        store.create_team_account(row)
        store.record_control_event(identity.get("id", "owner"), "account_created", row["id"])
        return {key: row[key] for key in ("id", "username", "role", "created_at", "last_login_at", "disabled_at")}

    @app.delete("/dashboard/accounts/{user_id}")
    def disable_dashboard_account(user_id: str, identity: Identity):
        require_admin(identity)
        account = store.account_by_id(user_id)
        if not account or account.get("workspace_id", DEFAULT_WORKSPACE_ID) != identity_workspace(identity):
            raise HTTPException(404, "Non-owner account not found in your workspace")
        if not store.disable_team_account(user_id, time.time()):
            raise HTTPException(404, "Non-owner account not found or already disabled")
        store.record_control_event(identity.get("id", "owner"), "account_disabled", user_id)
        return {"id": user_id, "disabled": True}

    @app.get("/dashboard/workspace")
    def dashboard_workspace(identity: Identity):
        require_workspace_manager(identity)
        workspace_id = identity_workspace(identity)
        connections = []
        for row in store.workspace_connections(workspace_id):
            peer_id = row["workspace_b"] if row["workspace_a"] == workspace_id else row["workspace_a"]
            peer = store.workspace_info(peer_id) or {}
            connections.append({"workspace_id": peer_id, "workspace_name": peer.get("name", peer_id),
                                "status": row["status"], "created_at": row["created_at"]})
        own = store.workspace_info(workspace_id) or {}
        return {"workspace_id": workspace_id, "workspace_name": own.get("name", workspace_id), "connections": connections,
                "invitations": store.workspace_invites(workspace_id)}

    @app.post("/dashboard/workspace-invitations", status_code=201)
    def create_workspace_invitation(body: WorkspaceInviteCreate, identity: Identity):
        require_workspace_manager(identity)
        now = time.time()
        code = "amw_" + secrets.token_urlsafe(32)
        workspace_id = identity_workspace(identity)
        active_invites = [row for row in store.workspace_invites(workspace_id)
                          if row.get("used_at") is None and row.get("revoked_at") is None and row.get("expires_at", 0) > now]
        if len(active_invites) >= 20:
            raise HTTPException(409, "Workspace has 20 active invitations; revoke or wait for one to expire")
        row = {"id": str(uuid4()), "code_hash": hashlib.sha256(code.encode()).hexdigest(),
               "workspace_id": workspace_id, "created_by": identity.get("id", "owner"),
               "created_at": now, "expires_at": now + body.expires_in_hours * 3600,
               "mode": body.mode}
        store.create_workspace_invite(row)
        store.record_control_event(identity.get("id", "owner"), "workspace_invitation_created", row["id"])
        return JSONResponse({"invitation": {key: row[key] for key in
                             ("id", "workspace_id", "created_at", "expires_at")},
                             "code": code,
                             "url": ("/join-member#invite=" if body.mode == "member" else "/join-workspace#invite=") + code,
                             "mode": body.mode,
                             "warning": ("One-time code. The recipient creates a member account inside this workspace."
                                         if body.mode == "member" else
                                         "One-time code. The recipient creates their own account and workspace.")},
                            status_code=201, headers={"Cache-Control": "no-store"})

    @app.post("/dashboard/member-invitations", status_code=201)
    def create_member_invitation(body: WorkspaceInviteCreate, identity: Identity):
        body.mode = "member"
        return create_workspace_invitation(body, identity)

    @app.delete("/dashboard/workspace-invitations/{invite_id}")
    def revoke_workspace_invitation(invite_id: str, identity: Identity):
        require_workspace_manager(identity)
        if not store.revoke_workspace_invite(identity_workspace(identity), invite_id, time.time()):
            raise HTTPException(404, "Active invitation not found")
        store.record_control_event(identity.get("id", "owner"), "workspace_invitation_revoked", invite_id)
        return {"id": invite_id, "revoked": True}

    @app.delete("/dashboard/workspace-connections/{peer_workspace_id}")
    def disconnect_workspace(peer_workspace_id: str, identity: Identity):
        require_workspace_manager(identity)
        if not store.disconnect_workspaces(identity_workspace(identity), peer_workspace_id, time.time()):
            raise HTTPException(404, "Workspace connection not found")
        store.record_control_event(identity.get("id", "owner"), "workspace_disconnected", peer_workspace_id)
        return {"workspace_id": peer_workspace_id, "disconnected": True}

    @app.post("/dashboard/workspace-connections", status_code=201)
    def accept_workspace_connection(body: WorkspaceConnectionAccept, identity: Identity):
        require_workspace_manager(identity)
        result = store.accept_existing_workspace_invite(
            hashlib.sha256(body.invite_code.encode()).hexdigest(), identity_workspace(identity), time.time())
        store.record_control_event(identity.get("id", "workspace-owner"), "workspace_connected", result["workspace_id"])
        return result | {"workspace_name": (store.workspace_info(result["workspace_id"]) or {}).get("name", result["workspace_id"])}

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

    @app.get("/a2a.md", response_class=PlainTextResponse, include_in_schema=False)
    def a2a_gateway_documentation():
        path = FilePath(__file__).parent / "docs" / "A2A-GATEWAY.md"
        return PlainTextResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/a2a-extension.md", response_class=PlainTextResponse, include_in_schema=False)
    def a2a_extension_documentation():
        path = FilePath(__file__).parent / "docs" / "A2A-EXTENSION-INSTANCE-ROUTING.md"
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

    @app.post("/join/secure", status_code=201)
    @app.post("/join", status_code=201)
    def join(body: AgentRegistration, request: Request, credentials: Credential):
        local_open = local_keyless_registration_request(request)
        registration = store.account_by_registration_key_hash(hashlib.sha256(credentials.credentials.encode()).hexdigest()) if credentials else None
        registrar = None
        owner = None
        if credentials is not None:
            if registration is not None:
                if registration.get("role") not in ("owner", "workspace_owner", "operator", "workspace_member"):
                    raise HTTPException(403, "This account cannot register agents")
                registration_seed = workspace_registration_seed(registration["workspace_id"])
                workspace_id = registration["workspace_id"]
                registrar = {"account_id": registration["user_id"], "username": registration["username"], "method": "account_registration_key"}
                owner = account_owner_view(registration["user_id"], registration["username"])
                require_owner_declaration(body.owner_name, owner)
            elif not join_key or not secrets.compare_digest(credentials.credentials.encode(), join_key.encode()):
                raise HTTPException(401, "Clave de acceso incorrecta o ausente", headers={"WWW-Authenticate": "Bearer"})
            else:
                registration_seed = hub_key
                workspace_id = DEFAULT_WORKSPACE_ID
                registrar = {"method": "server_join_key", "username": "Servidor"}
        elif local_open:
            registration_seed = local_instance_seed
            workspace_id = DEFAULT_WORKSPACE_ID
            registrar = {"method": "local_loopback", "username": "Este equipo"}
        elif not join_key:
            raise HTTPException(503, "Self-registration is not configured")
        else:
            raise HTTPException(401, "Clave de acceso incorrecta o ausente", headers={"WWW-Authenticate": "Bearer"})
        result = create_registration(body, registration_seed, workspace_id,
                                     instance_token=request.headers.get("x-mailbox-instance-token"),
                                     recovery_secret=request.headers.get("x-mailbox-recovery-secret"),
                                     require_existing_proof=True,
                                     forbidden_secret=credentials.credentials if credentials else None,
                                     registrar=registrar)
        # A shared join key authorizes registration only; each chat gets its own bearer token.
        from fastapi.responses import JSONResponse
        payload = {**result, "instructions_url": "/connect.md"}
        if owner:
            payload["owner"] = owner
        return JSONResponse(payload, status_code=201, headers={"Cache-Control": "no-store"})

    @app.get("/registration/owner")
    def registration_owner(credentials: Credential):
        """Con la clave de registro se leen los datos del dueño: el agente debe declararlos al registrarse."""
        if credentials is None:
            raise HTTPException(401, "Se requiere la clave de registro del dueño")
        registration = store.account_by_registration_key_hash(hashlib.sha256(credentials.credentials.encode()).hexdigest())
        if registration is None:
            raise HTTPException(403, "Esta credencial no es una clave de registro de cuenta; "
                                     "pide al dueño la clave de registro de su cuenta")
        return {"owner": account_owner_view(registration["user_id"], registration["username"]),
                "workspace_id": registration["workspace_id"],
                "requirement": "Envía owner_name con el username o el display_name del dueño; si no coincide, "
                               "el registro responde 422",
                "endpoint": "/join/secure"}

    @app.get("/health")
    def health():
        store.ping()
        return {"ok": True, "storage": backend, "version": app.version}

    @app.get("/capabilities")
    def capabilities():
        return {"version": APP_VERSION, "profiles": True, "private_instance_inboxes": True,
                "secure_registration": {"version": 1, "recovery_secret": True,
                                        "secure_paths": ["/join/secure", "/agents/secure", "/profiles/{id}/instances/secure"]},
                "account_registration_keys": {"enabled": True, "endpoint": "/account/registration-key",
                                               "scope": "per-account workspace", "shown_once": True,
                                               "rotation_revokes_previous": True,
                                               "owner_endpoint": "/registration/owner",
                                               "registration_requires_owner_name": True,
                                               "note": "An agent registering with this key must declare owner_name "
                                                       "(username or display name of the key's owner); read it first with "
                                                       "GET /registration/owner"},
                "account_signup": {"enabled": True, "endpoint": "/auth/signup",
                                    "requires_owner_setup": True, "creates_private_workspace": True},
                "workspaces": {"enabled": True, "one_time_invites": True,
                               "directory": "own workspace and accepted connections",
                               "message_access": "only between agents in the same or connected workspaces",
                               "disconnect_revokes_new_cross_workspace_messages": True},
                "local_open_registration": {"enabled": local_first_run, "requires_user_account": False,
                                            "requires_join_key": False,
                                            "scope": "loopback client and local host only"},
                "bound_replies": True, "transactional_final_reply": True, "idempotent_delivery": True,
                "control_roles": ["owner", "workspace_owner", "workspace_member", "operator", "observer"], "local_file_bank": True,
                "peer_discovery": {"enabled": True, "agent_card_description_chars": 500,
                                   "capabilities_per_card": 20,
                                   "directory_scope": "same workspace and explicitly connected workspaces"},
                "inbox_wait": {"enabled": True, "max_timeout_seconds": 25, "slots_per_process": 12,
                               "scope": "new unread messages for the authenticated instance only",
                               "mode": "opt-in; an open request can keep hosted compute active"},
                "attachments": {"max_count": 3, "total_bytes": attachment_limit},
                "a2a": {"enabled": True, "version": A2A_VERSION, "binding": "HTTP+JSON",
                        "agent_card": "/.well-known/agent-card.json", "base_path": "/a2a",
                        "streaming": False, "push_notifications": False},
                "model_wake": "host-specific; event delivery does not itself start a model turn"}

    @app.post("/agents/secure", status_code=201)
    @app.post("/agents", status_code=201)
    def register(body: AgentRegistration, request: Request, identity: Identity):
        if not can_manage_agents(identity):
            raise HTTPException(403, "Agent registration requires an owner or operator key")
        workspace_id = identity_workspace(identity)
        registrar = {"account_id": identity.get("id"), "username": identity.get("username", ""),
                     "method": "account_registration_key" if identity.get("registration_key") else "dashboard_session"}
        registration_seed = (workspace_registration_seed(workspace_id)
                             if identity.get("registration_key") else
                             (local_instance_seed if local_first_run else hub_key))
        if workspace_id != DEFAULT_WORKSPACE_ID:
            registration_seed = hmac.new(hub_key.encode(), workspace_id.encode(), hashlib.sha256).hexdigest()
        authorization = request.headers.get("authorization", "")
        bearer_value = authorization[7:] if authorization.lower().startswith("bearer ") else None
        result = create_registration(body, registration_seed, workspace_id,
                                     instance_token=request.headers.get("x-mailbox-instance-token"),
                                     recovery_secret=request.headers.get("x-mailbox-recovery-secret"),
                                     require_existing_proof=True, forbidden_secret=bearer_value,
                                     registrar=registrar)
        if identity.get("control_user"):
            store.record_control_event(identity["id"], "agent_registered", result["agent"]["id"])
        return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})

    @app.get("/stats")
    def stats(identity: Identity):
        return {**store.workspace_stats(identity_workspace(identity), time.time() - online_seconds),
                "online_window_seconds": online_seconds}

    @app.get("/profiles")
    def list_profiles(identity: Identity):
        return profiles_directory(visible_workspace_ids(identity))

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
        return profile_instances(profile_id, visible_workspace_ids(identity))

    @app.delete("/profiles/{profile_id}")
    def delete_profile(profile_id: str, credentials: Credential):
        store.authorize_profile(profile_id, credentials.credentials if credentials else "")
        store.delete_empty_profile(profile_id)
        return {"profile_id": profile_id, "deleted": True}

    @app.get("/profiles/{profile_id}/activity")
    def get_profile_activity(profile_id: str, identity: Identity):
        workspace_owner = identity.get("workspace_scoped") and identity.get("role") == "workspace_owner"
        if identity["admin"] or workspace_owner:
            profile = store.profile_get(profile_id)
            if not profile or (profile.get("workspace_id") or DEFAULT_WORKSPACE_ID) != identity_workspace(identity):
                raise HTTPException(404, "Profile not found in your workspace")
            return store.profile_activity(None, profile_id, True)
        return store.profile_activity(identity.get("id"), profile_id, False)

    @app.post("/profiles/{profile_id}/instances/secure", status_code=201)
    @app.post("/profiles/{profile_id}/instances", status_code=201)
    def enroll_instance(profile_id: str, body: InstanceRegistration, request: Request, credentials: Credential):
        profile = store.authorize_profile(profile_id, credentials.credentials if credentials else "")
        session_key = body.session_key or secrets.token_urlsafe(24)
        identifier = str(uuid5(UUID(profile_id), session_key))
        proof = request.headers.get("x-mailbox-instance-token")
        recovery_secret = request.headers.get("x-mailbox-recovery-secret")
        if recovery_secret:
            validate_recovery_secret(recovery_secret, (credentials.credentials,))
        if proof and recovery_secret:
            raise HTTPException(422, "Provide either the instance token or recovery secret")
        existing = store.get_agent(identifier)
        if existing:
            token = existing_instance_token(existing, identifier, proof, recovery_secret)
            if not token:
                raise HTTPException(409, "Existing session requires its original instance token or recovery secret")
        else:
            if proof:
                raise HTTPException(422, "An instance token can only resume an existing session")
            token = recovery_token(recovery_secret, identifier) if recovery_secret else secrets.token_urlsafe(32)
        now = time.time()
        row = dict(id=identifier, name=body.instance_name, kind=profile["kind"],
                   project=body.project or profile["project"], created_at=now, last_seen=now,
                   description=body.description, capabilities=body.capabilities,
                   token_hash=hashlib.sha256(token.encode()).hexdigest(),
                   recovery_hash=hashlib.sha256(recovery_secret.encode()).hexdigest() if recovery_secret else None,
                   created_by_account_id=profile.get("created_by_account_id"),
                   created_by_username=profile.get("created_by_username", ""),
                   registration_method="profile_enrollment",
                   workspace_id=profile.get("workspace_id") or DEFAULT_WORKSPACE_ID)
        created = store.enroll_profile_instance(profile_id, row)
        if not created and not (proof or recovery_secret):
            raise HTTPException(409, "Existing session requires its original instance token or recovery secret")
        agent = public_agent(store.get_agent(identifier), time.time())
        return JSONResponse({"agent": agent, "token": token, "resumed": not created,
                             "session_id": session_key,
                             "onboarding": onboarding_handoff(agent, session_key)}, status_code=201,
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
        return discover_agents(query, visible_workspace_ids(identity))

    @app.get("/agents")
    def list_agents(identity: Identity, online: bool | None = None,
                    limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        now = time.time()
        agents = directory_agents(now, visible_workspace_ids(identity))
        if online is not None:
            agents = [row for row in agents if row["online"] == online]
        return {"agents": agents[offset:offset + limit], "total": len(agents), "limit": limit, "offset": offset}

    @app.get("/agents/me")
    def me(identity: Identity):
        row = store.get_agent(agent_id(identity))
        result = public_agent(row, time.time())
        result["owner"] = account_owner_view(row.get("created_by_account_id"), row.get("created_by_username", ""))
        return result

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
        return JSONResponse(create_agent_api_key(identifier, body.name, identity["credential_hash"]), status_code=201,
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
        if fields.get("name", "") is None:
            fields.pop("name", None)
        fields = {key: value if value is not None else ([] if key == "capabilities" else "")
                  for key, value in fields.items()}
        if not fields:
            raise HTTPException(422, "Provide name, description, capabilities and/or chat_path")
        row = store.update_agent_card(agent_id(identity), fields)
        return public_agent(row, time.time())

    @app.patch("/dashboard/agents/{identifier}")
    def rename_dashboard_agent(identifier: Annotated[str, Path(min_length=1, max_length=100,
                                                                pattern=r"^[A-Za-z0-9_-]+$")],
                               body: AgentRename, identity: Identity):
        require_manage_own_agent(identity, identifier)
        row = store.update_agent_card(identifier, {"name": body.name})
        if identity.get("control_user"):
            store.record_control_event(identity["id"], "agent_renamed", identifier)
        return public_agent(row, time.time())

    @app.post("/dashboard/agents/{identifier}/rotate-token")
    def rotate_dashboard_agent_token(identifier: Annotated[str, Path(min_length=1, max_length=100,
                                                                     pattern=r"^[A-Za-z0-9_-]+$")],
                                     request: Request, identity: Identity):
        require_workspace_manager(identity)
        require_manage_own_agent(identity, identifier)
        recovery_secret = request.headers.get("x-mailbox-recovery-secret", "")
        authorization = request.headers.get("authorization", "")
        bearer_value = authorization[7:] if authorization.lower().startswith("bearer ") else None
        validate_recovery_secret(recovery_secret, (bearer_value,))
        agent = store.get_agent(identifier)
        profile = store.profile_get(agent["profile_id"]) if agent and agent.get("profile_id") else None
        if agent and secrets.compare_digest(hashlib.sha256(recovery_secret.encode()).hexdigest(), agent.get("token_hash", "")):
            raise HTTPException(422, "Generate a separate random recovery secret for this instance")
        if profile and secrets.compare_digest(hashlib.sha256(recovery_secret.encode()).hexdigest(), profile.get("enrollment_hash", "")):
            raise HTTPException(422, "Generate a separate random recovery secret for this instance")
        expected_epoch_raw = request.headers.get("x-mailbox-expected-epoch", "")
        if not re.fullmatch(r"[0-9]{1,10}", expected_epoch_raw):
            raise HTTPException(422, "Provide the instance credential epoch from your dashboard")
        expected_epoch = int(expected_epoch_raw)
        token = recovery_token(recovery_secret, identifier)
        now = time.time()
        rotated = store.rotate_agent_token(identifier, hashlib.sha256(token.encode()).hexdigest(),
                                           hashlib.sha256(recovery_secret.encode()).hexdigest(), expected_epoch, now)
        if rotated:
            store.record_control_event(identity.get("id", "owner"), "agent_token_rotated", identifier)
        return JSONResponse({"agent": public_agent(store.get_agent(identifier), now), "token": token,
                             "replayed": not rotated,
                             "warning": "Update this instance's private config. Its previous token and named API keys are invalid."},
                            headers={"Cache-Control": "no-store"})

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
        require_manage_own_agent(identity, identifier)
        if not store.unregister(identifier):
            raise HTTPException(404, "Agent not found")
        if identity.get("control_user"):
            store.record_control_event(identity["id"], "agent_removed", identifier)
        return {"id": identifier, "deleted": True,
                "messages_and_attachments_deleted": True}

    @app.post("/heartbeat")
    def heartbeat(identity: Identity):
        return {"agent_id": agent_id(identity), "online": True, "online_window_seconds": online_seconds}

    @app.post("/agents/me/listener")
    def report_my_listener(body: ListenerReport, identity: Identity):
        identifier = agent_id(identity)
        now = time.time()
        store.report_listener(identifier, now, 2 * body.interval_seconds + 30, body.wake_configured)
        return public_agent(store.get_agent(identifier), now)

    @app.post("/messages", status_code=201)
    def send_message(body: Message, identity: Identity):
        sender = agent_id(identity)
        if body.delivery_mode == "wake" and body.in_reply_to is not None:
            raise HTTPException(422, "Only a new message can use delivery_mode='wake'")
        if body.wake_backlog and body.delivery_mode != "wake":
            raise HTTPException(422, "wake_backlog requires delivery_mode='wake'")
        attachments = prepare_attachments(body.attachments, attachment_limit)
        if not body.text.strip() and not attachments:
            raise HTTPException(422, "Message text cannot be blank")
        return store.send(sender, body, attachments)

    def a2a_origin(request):
        return (os.environ.get("MAILBOX_PUBLIC_URL") or str(request.base_url)).rstrip("/")

    def a2a_http_problem(error):
        status = error.status_code
        name = {401: "Unauthenticated", 403: "PermissionDenied", 404: "TaskNotFoundError",
                409: "TaskNotCancelableError", 413: "ContentTypeNotSupportedError",
                415: "ContentTypeNotSupportedError", 422: "InvalidParams", 501: "UnsupportedOperationError"}.get(
                    status, "InternalError")
        detail = error.detail if status < 500 else "A2A gateway request failed"
        return a2a_problem(status, name, str(detail))

    def a2a_sender(request, credentials):
        if not credentials or credentials.scheme.casefold() != "bearer":
            raise HTTPException(401, "A2A requires this caller's Agent Mailbox Bearer token",
                                headers={"WWW-Authenticate": "Bearer"})
        return agent_id(authenticate(credentials, request))

    def a2a_check_headers(request):
        if request.headers.get("A2A-Version") != A2A_VERSION:
            return a2a_problem(400, "VersionNotSupportedError",
                               f"This Agent Card supports A2A-Version {A2A_VERSION}")
        extensions = {value.strip() for value in request.headers.get("A2A-Extensions", "").split(",")
                      if value.strip()}
        if ROUTING_EXTENSION not in extensions:
            return a2a_problem(400, "ExtensionSupportRequiredError",
                               f"Declare the required extension in A2A-Extensions: {ROUTING_EXTENSION}")
        return None

    def a2a_task_for_request(requester, message_id, history_length=50, include_artifacts=True,
                             include_history=True):
        original = store.get_message(requester, message_id)
        if (original["from_agent_id"] != requester or original.get("in_reply_to") is not None or
                original.get("message_type") != "request"):
            raise HTTPException(404, "Task not found")
        thread = store.thread_messages(requester, message_id, after_id=0, limit=200)
        return a2a_task_document(original, requester, thread,
                                 lambda item_id, index: store.get_attachment(requester, item_id, index),
                                 history_length=history_length, include_artifacts=include_artifacts,
                                 include_history=include_history)

    def a2a_task_page(requester, after_id, page_size, mailbox_status=None, thread_id=None):
        cursor = after_id
        found = []
        while len(found) <= page_size:
            page = store.mail_page(requester, "outbox", after_id=cursor, limit=200,
                                   status=mailbox_status, thread_id=thread_id)
            for row in page["messages"]:
                if row.get("message_type") == "request" and row.get("in_reply_to") is None:
                    found.append(row)
                    if len(found) > page_size:
                        return found[:page_size], found[page_size - 1]["id"], True
            if not page["has_more"]:
                return found, page["next_after_id"], False
            if page["next_after_id"] <= cursor:
                return found, page["next_after_id"], False
            cursor = page["next_after_id"]
        return found[:page_size], found[page_size - 1]["id"], True

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    def a2a_public_agent_card(request: Request):
        card = a2a_agent_card(a2a_origin(request), APP_VERSION)
        return JSONResponse(card, headers={"Cache-Control": "public, max-age=300",
                                           "X-Content-Type-Options": "nosniff"})

    @app.post("/a2a/message:send", include_in_schema=False)
    async def a2a_send_message(request: Request, credentials: Credential):
        header_problem = a2a_check_headers(request)
        if header_problem:
            return header_problem
        try:
            sender = a2a_sender(request, credentials)
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
                raise HTTPException(400, "A2A SendMessageRequest must include a message object")
            config = body.get("configuration") or {}
            if not isinstance(config, dict):
                raise HTTPException(400, "A2A configuration must be an object")
            if config.get("returnImmediately") is not True:
                raise HTTPException(400, "Agent Mailbox tasks are asynchronous; set configuration.returnImmediately=true")
            message = body["message"]
            if message.get("role") != "ROLE_USER":
                raise HTTPException(400, "A2A gateway accepts client messages with role ROLE_USER only")
            if message.get("taskId"):
                raise HTTPException(501, "This gateway accepts one-shot mailbox tasks; send a new task for follow-up work")
            a2a_message_id = message.get("messageId")
            if not isinstance(a2a_message_id, str) or not a2a_message_id.strip() or len(a2a_message_id) > 300:
                raise HTTPException(400, "A2A message.messageId is required and must be at most 300 characters")
            message_extensions = message.get("extensions") or []
            if not isinstance(message_extensions, list) or ROUTING_EXTENSION not in message_extensions:
                raise HTTPException(400, "A2A Message.extensions must include the Agent Mailbox routing extension")
            request_metadata = body.get("metadata") or {}
            message_metadata = message.get("metadata") or {}
            if not isinstance(request_metadata, dict) or not isinstance(message_metadata, dict):
                raise HTTPException(400, "A2A metadata fields must be objects")
            routing = request_metadata.get(ROUTING_EXTENSION)
            if routing is None:
                routing = message_metadata.get(ROUTING_EXTENSION)
            if not isinstance(routing, dict):
                raise HTTPException(400, f"SendMessageRequest.metadata must include {ROUTING_EXTENSION}")
            recipient = routing.get("recipientInstanceId")
            if not isinstance(recipient, str) or not recipient.strip():
                raise HTTPException(400, "A2A routing extension requires an exact recipientInstanceId")
            if not store.get_agent(recipient):
                raise HTTPException(404, "Recipient instance not found")

            parts = message.get("parts")
            if not isinstance(parts, list) or not parts:
                raise HTTPException(400, "A2A Message.parts must contain text or file parts")
            text_parts, attachments = [], []
            for part in parts:
                if not isinstance(part, dict):
                    raise HTTPException(400, "Each A2A Part must be an object")
                content_fields = [key for key in ("text", "raw", "url", "data") if key in part]
                if len(content_fields) != 1:
                    raise HTTPException(400, "Each A2A Part must contain exactly one supported content field")
                kind = content_fields[0]
                if kind == "text":
                    if not isinstance(part["text"], str):
                        raise HTTPException(400, "A2A text parts must contain strings")
                    text_parts.append(part["text"])
                elif kind == "data":
                    text_parts.append(json.dumps(part["data"], ensure_ascii=False, separators=(",", ":")))
                elif kind == "url":
                    raise HTTPException(415, "Remote A2A file URLs are disabled; send file bytes to avoid server-side URL fetching")
                else:
                    if not isinstance(part["raw"], str):
                        raise HTTPException(400, "A2A Part.raw must be base64 text")
                    attachments.append(Attachment(filename=part.get("filename") or "attachment",
                                                  media_type=part.get("mediaType") or "application/octet-stream",
                                                  data_base64=part["raw"]))
            delivery_mode = routing.get("deliveryMode", "inbox")
            wake_backlog = routing.get("wakeBacklog", False)
            if delivery_mode not in ("inbox", "wake") or not isinstance(wake_backlog, bool):
                raise HTTPException(400, "A2A routing extension has invalid deliveryMode or wakeBacklog")
            if wake_backlog and delivery_mode != "wake":
                raise HTTPException(400, "wakeBacklog=true requires deliveryMode=wake")
            subject = routing.get("subject", "")
            if not isinstance(subject, str) or len(subject) > 180:
                raise HTTPException(400, "A2A subject must be a string up to 180 characters")
            body_message = Message(
                to_agent_id=recipient, text="\n".join(text_parts), subject=subject,
                message_type="request", delivery_mode=delivery_mode, wake_backlog=wake_backlog,
                idempotency_key="a2a-" + hashlib.sha256(a2a_message_id.encode("utf-8")).hexdigest(),
                attachments=attachments)
            prepared = prepare_attachments(body_message.attachments, attachment_limit)
            if not body_message.text.strip() and not prepared:
                raise HTTPException(422, "A2A task must include text or a file")
            sent = store.send(sender, body_message, prepared)
            task = a2a_task_document(sent, sender, {"messages": [sent]},
                                     lambda item_id, index: store.get_attachment(sender, item_id, index),
                                     history_length=0, include_artifacts=False, include_history=False)
            return JSONResponse({"task": task}, media_type="application/a2a+json")
        except HTTPException as error:
            if error.status_code == 409:
                return a2a_problem(400, "InvalidParams", str(error.detail))
            return a2a_http_problem(error)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return a2a_problem(400, "InvalidParams", "Malformed A2A SendMessageRequest")

    @app.get("/a2a/tasks/{task_id}", include_in_schema=False)
    def a2a_get_task(task_id: str, request: Request, credentials: Credential,
                     history_length: int = Query(50, alias="historyLength", ge=0, le=200)):
        header_problem = a2a_check_headers(request)
        if header_problem:
            return header_problem
        try:
            sender = a2a_sender(request, credentials)
            message_id = a2a_mailbox_id_from_task(task_id)
            if message_id is None:
                raise HTTPException(404, "Task not found")
            task = a2a_task_for_request(sender, message_id, history_length=history_length,
                                        include_artifacts=True, include_history=history_length != 0)
            return JSONResponse({"task": task}, media_type="application/a2a+json")
        except HTTPException as error:
            return a2a_http_problem(error)

    @app.get("/a2a/tasks", include_in_schema=False)
    def a2a_list_tasks(request: Request, credentials: Credential,
                       context_filter: str | None = Query(None, alias="contextId"),
                       task_status: str | None = Query(None, alias="status"),
                       page_size: int = Query(50, alias="pageSize", ge=1, le=100),
                       page_token: str = Query("", alias="pageToken"),
                       history_length: int = Query(0, alias="historyLength", ge=0, le=200),
                       include_artifacts: bool = Query(False, alias="includeArtifacts")):
        header_problem = a2a_check_headers(request)
        if header_problem:
            return header_problem
        try:
            sender = a2a_sender(request, credentials)
            mailbox_status = a2a_mailbox_status_from_task_state(task_status) if task_status else None
            if task_status and mailbox_status is None:
                raise HTTPException(400, "Unsupported A2A task status filter")
            thread_id = a2a_thread_from_context(context_filter) if context_filter else None
            if context_filter and thread_id is None:
                raise HTTPException(400, "Unknown Agent Mailbox contextId")
            after_id = a2a_decode_page_token(page_token)
            rows, next_cursor, has_more = a2a_task_page(sender, after_id, page_size, mailbox_status, thread_id)
            total_size = store.count_outbox_requests(sender, mailbox_status, thread_id)
            tasks = []
            for row in rows:
                if history_length or include_artifacts:
                    task = a2a_task_for_request(sender, row["id"], history_length=history_length,
                                                include_artifacts=include_artifacts,
                                                include_history=history_length != 0)
                else:
                    task = a2a_task_document(row, sender, {"messages": []},
                                             lambda item_id, index: store.get_attachment(sender, item_id, index),
                                             history_length=0, include_artifacts=False, include_history=False)
                tasks.append(task)
            return JSONResponse({"tasks": tasks,
                                 "nextPageToken": a2a_encode_page_token(next_cursor) if has_more else "",
                                 "pageSize": page_size, "totalSize": total_size},
                                media_type="application/a2a+json")
        except HTTPException as error:
            return a2a_http_problem(error)
        except ValueError:
            return a2a_problem(400, "InvalidParams", "Malformed A2A task filter or page token")

    @app.post("/a2a/tasks/{task_id}:cancel", include_in_schema=False)
    async def a2a_cancel_task(task_id: str, request: Request, credentials: Credential):
        header_problem = a2a_check_headers(request)
        if header_problem:
            return header_problem
        try:
            sender = a2a_sender(request, credentials)
            message_id = a2a_mailbox_id_from_task(task_id)
            if message_id is None:
                raise HTTPException(404, "Task not found")
            raw = await request.body()
            body = json.loads(raw) if raw else {}
            body_id = body.get("id") if isinstance(body, dict) else None
            if body_id and body_id != task_id:
                raise HTTPException(400, "CancelTask body id does not match the URL task ID")
            store.cancel_outgoing_request(sender, message_id, note="Cancelled by A2A task requester")
            task = a2a_task_for_request(sender, message_id, history_length=0,
                                        include_artifacts=False, include_history=False)
            return JSONResponse({"task": task}, media_type="application/a2a+json")
        except HTTPException as error:
            return a2a_http_problem(error)
        except json.JSONDecodeError:
            return a2a_problem(400, "InvalidParams", "Malformed A2A CancelTask request")

    @app.post("/a2a/message:stream", include_in_schema=False)
    def a2a_stream_unsupported(request: Request):
        return a2a_problem(501, "UnsupportedOperationError",
                           "Streaming is not enabled by this Agent Mailbox A2A gateway")

    @app.get("/a2a/tasks/{task_id}:subscribe", include_in_schema=False)
    def a2a_subscribe_unsupported(task_id: str, request: Request):
        return a2a_problem(400, "UnsupportedOperationError",
                           "Task streaming is not enabled; poll GET /a2a/tasks/{id}")

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
        if identity.get("workspace_scoped") and identity.get("role") == "workspace_owner":
            if not store.message_in_workspace(message_id, identity_workspace(identity)):
                raise HTTPException(404, "Message not found in your workspace")
            return store.delete_message(identity.get("id", ""), message_id, admin=True)
        if identity.get("admin") and not store.message_in_workspace(message_id, identity_workspace(identity)):
            raise HTTPException(404, "Message not found in your workspace")
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

