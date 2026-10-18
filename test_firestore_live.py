r"""Exercise real Firestore; pass a short-lived gcloud access token via stdin.

gcloud auth print-access-token | .\.venv\Scripts\python.exe test_firestore_live.py
Only documents belonging to the two generated test agents are cleaned up.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import base64
import os
import secrets
import sys
import time
from uuid import uuid4

from fastapi import HTTPException
from google.oauth2.credentials import Credentials

from server import Message
from stores import FirestoreStore


def main():
    token = sys.stdin.read().strip()
    if not token:
        raise RuntimeError("Pass a gcloud access token through stdin")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("Set GOOGLE_CLOUD_PROJECT before running the live Firestore test")
    database = os.environ.get("MAILBOX_FIRESTORE_DATABASE", "agent-mailbox")
    store = FirestoreStore(project, database, credentials=Credentials(token))
    agents, agent_tokens, profiles = [], [], []
    try:
        store.initialize()
        for name in ("Live test A", "Live test B"):
            now, agent_token = time.time(), secrets.token_urlsafe(32)
            row = dict(id=str(uuid4()), name=name, kind="test", project="deployment-verification",
                       token_hash=hashlib.sha256(agent_token.encode()).hexdigest(), created_at=now, last_seen=now)
            store.register(row)
            agents.append(row)
            agent_tokens.append(agent_token)
        a, b = [row["id"] for row in agents]
        assert store.authenticate(hashlib.sha256(agent_tokens[0].encode()).hexdigest()) == a
        assert store.authenticate(hashlib.sha256(b"invalid-token").hexdigest()) is None
        question = store.send(a, Message(to_agent_id=b, text="Pregunta real desde el despliegue"))
        assert store.inbox(b, True, 0, 10)[0]["id"] == question["id"]
        assert store.inbox(b, True, 0, 10)[0]["id"] == question["id"]
        reply = store.send(b, Message(to_agent_id=a, text="Respuesta real", in_reply_to=question["id"]))
        assert store.inbox(a, True, 0, 10)[0]["id"] == reply["id"]
        assert store.inbox_count(b) == {"total": 1, "pending": 1}
        try:
            store.acknowledge(a, question["id"])
            raise AssertionError("Another agent acknowledged a private message")
        except HTTPException as error:
            assert error.status_code == 404
        store.acknowledge(b, question["id"])
        store.acknowledge(b, question["id"])
        assert store.inbox(b, True, 0, 10) == []
        assert len(store.inbox(b, False, 0, 10)) == 1
        with ThreadPoolExecutor(max_workers=4) as pool:
            sent = list(pool.map(lambda n: store.send(a, Message(to_agent_id=b, text=f"parallel {n}")), range(8)))
        assert len({row["id"] for row in sent}) == 8
        first = store.inbox(b, True, 0, 3)
        remaining = store.inbox(b, True, first[-1]["id"], 100)
        assert len(first + remaining) == 8
        image_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/nWQAAAAASUVORK5CYII="
        )
        image_message = store.send(a, Message(to_agent_id=b, text="Imagen de prueba"), [{
            "filename": "pixel.png", "media_type": "image/png", "size_bytes": len(image_bytes),
            "data": image_bytes,
        }])
        assert store.inbox(b, True, image_message["id"] - 1, 1)[0]["attachments"][0]["filename"] == "pixel.png"
        assert store.get_attachment(b, image_message["id"], 0)["data"] == image_bytes
        # A fresh client sees the same persistent documents and acknowledgement.
        second = FirestoreStore(project, database, credentials=Credentials(token))
        assert second.inbox_count(b) == {"total": 10, "pending": 9}
        stats = second.stats(time.time() - 90)
        assert stats["agents_total"] >= 2 and stats["messages_total"] >= 11
        unlinked = store.delete_message(a, question["id"])
        assert unlinked["deleted"] and unlinked["related_replies_unlinked"] == 1
        assert store.get_message(a, reply["id"])["in_reply_to"] is None
        purged = store.delete_message(b, image_message["id"])
        assert purged["deleted"]
        try:
            store.get_attachment(b, image_message["id"], 0)
            raise AssertionError("Deleted attachment was still available")
        except HTTPException as error:
            assert error.status_code == 404
        sent_cutoff = max(message["id"] for message in sent)
        first_page = store.delete_inbox(b, question["id"], sent_cutoff, 3)
        assert first_page["deleted_count"] == 3 and first_page["has_more"]
        remaining = store.delete_inbox(b, first_page["next_after_id"], sent_cutoff, 10)
        assert remaining["deleted_count"] == 5 and not remaining["has_more"]
        profile, enrollment_token = store.create_profile(a, "Temporary live profile", "test", "deployment-verification")
        profiles.append(profile["id"])
        now = time.time()
        child = dict(id=str(uuid4()), name="Live test child", kind="test", project="deployment-verification",
                     token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(), created_at=now, last_seen=now)
        assert store.enroll_profile_instance(profile["id"], child)
        agents.append(child)
        assert not store.enroll_profile_instance(profile["id"], child)
        store.send(b, Message(to_agent_id=a, subject="Private live subject", text="private-live-body"))
        activity = store.profile_activity(child["id"], profile["id"])
        assert len(activity["instances"]) == 2 and "private-live-body" not in str(activity)
        assert "Private live subject" not in str(activity)
        task = store.send(child["id"], Message(to_agent_id=b, subject="Bound live request", text="Inspect exact component"))
        answer = Message(to_agent_id=child["id"], text="Observed facts", in_reply_to=task["id"])
        final = store.send(b, answer, finalize={"status": "completed"})
        assert final["thread_id"] == task["thread_id"] and final["message_type"] == "reply"
        assert store.get_message(b, task["id"])["acknowledged_at"] is not None
        try:
            store.send(b, answer, finalize={"status": "completed"})
            raise AssertionError("Duplicate final reply was accepted")
        except HTTPException as error:
            assert error.status_code == 409
        assert len(store.thread_messages(b, task["id"])["messages"]) == 2
        with ThreadPoolExecutor(max_workers=1) as pool:
            wait = pool.submit(store.wait_for_mail, child['id'], final['id'], 10)
            time.sleep(.4)
            retry = Message(to_agent_id=child['id'], text="Private event", idempotency_key="live-retry")
            event = store.send(a, retry)
            assert wait.result(timeout=20)['message_id'] == event['id']
        assert store.send(a, retry)['id'] == event['id']
        store.delete_message(a, event['id'])
        try:
            store.send(a, retry)
            raise AssertionError("Deleted idempotent message resurrected")
        except HTTPException as error:
            assert error.status_code == 410
        print("PASS: Firestore persistence, profiles/enrollment, metadata privacy, threads, atomic final replies, duplicate rejection, attachments and deletion")
    finally:
        for agent in agents:
            query = store.where(store.messages, "to_agent_id", "==", agent["id"])
            for document in query.stream(timeout=15):
                message = document.to_dict()
                for attachment_index, attachment in enumerate(message.get("attachments", [])):
                    for chunk_index in range(attachment["chunks"]):
                        key = f"{message['id']:020d}_{attachment_index:04d}_{chunk_index:04d}"
                        store.attachment_chunks.document(key).delete(timeout=15)
                document.reference.delete(timeout=15)
            store.unregister(agent["id"])
        for profile_id in profiles:
            store.delete_empty_profile(profile_id)
        store.client.close()


if __name__ == "__main__":
    main()

