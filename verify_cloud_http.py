"""Verify the deployed API; keep only local credentials for the two test agents."""

import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def cleanup():
    from google.oauth2.credentials import Credentials
    from stores import FirestoreStore
    record = json.loads((Path(__file__).resolve().parent / ".local" / "cloud-verification.json").read_text(encoding="utf-8"))
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("Set GOOGLE_CLOUD_PROJECT before running the Firestore cleanup")
    store = FirestoreStore(project, os.environ.get("MAILBOX_FIRESTORE_DATABASE", "agent-mailbox"),
                           credentials=Credentials(sys.stdin.read().strip()))
    try:
        for registration in record["agents"]:
            identifier = registration["agent"]["id"]
            agent = store.get_agent(identifier)
            if agent is None:
                continue
            if agent["kind"] != "test" or agent["project"] != "cloud-http-verification":
                raise RuntimeError("Refusing to remove an agent outside this verification")
            for document in store.where(store.messages, "to_agent_id", "==", identifier).stream(timeout=15):
                document.reference.delete(timeout=15)
            store.agents.document(identifier).delete(timeout=15)
        print("Removed only the recorded HTTP verification agents and their inbox messages")
    finally:
        store.client.close()


def main():
    base = sys.argv[1].rstrip("/")
    directory = Path(__file__).resolve().parent / ".local"
    admin = (directory / "admin-key").read_text(encoding="utf-8-sig").strip()
    join_key = (directory / "join-key").read_text(encoding="utf-8-sig").strip()

    def request(method, path, token=None, body=None, expected=200):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        payload = None if body is None else json.dumps(body).encode()
        try:
            with urlopen(Request(base + path, data=payload, headers=headers, method=method), timeout=90) as response:
                status, data = response.status, json.load(response)
        except HTTPError as error:
            with error:
                status, data = error.code, error.read().decode()
        if status != expected:
            raise AssertionError(f"{method} {path}: expected {expected}, got {status}: {data}")
        return data

    health = request("GET", "/health")
    assert health["storage"] == "firestore"
    request("GET", "/agents", expected=401)
    request("GET", "/inbox", "invalid-token", expected=401)
    request("GET", "/inbox", join_key, expected=401)
    request("POST", "/join", "wrong-key", {"name": "not registered"}, 401)
    for path in ("/", "/connect.md"):
        with urlopen(base + path, timeout=90) as response:
            instructions = response.read().decode()
            assert "/join" in instructions and join_key not in instructions and admin not in instructions
    agents = []
    credentials_file = directory / "cloud-verification.json"
    try:
        for name in ("Verificación HTTP A", "Verificación HTTP B"):
            agents.append(request("POST", "/join", join_key, {"name": name, "kind": "test", "project": "cloud-http-verification"}, 201))
        credentials_file.write_text(json.dumps({"url": base, "agents": agents}, indent=2), encoding="utf-8")
        a, b = agents
        at, bt = a["token"], b["token"]
        aid, bid = a["agent"]["id"], b["agent"]["id"]
        listing = request("GET", "/agents?online=true", at)
        assert {aid, bid}.issubset({row["id"] for row in listing["agents"]})
        assert all("token_hash" not in row and "token" not in row for row in listing["agents"])
        questions = [request("POST", "/messages", at, {"to_agent_id": bid, "text": f"Prueba pública {n}"}, 201) for n in range(3)]
        inbox = request("GET", "/inbox?limit=2", bt)
        assert inbox["has_more"] and len(inbox["messages"]) == 2
        remaining = request("GET", f'/inbox?after_id={inbox["next_after_id"]}', bt)
        assert len(remaining["messages"]) == 1
        assert request("GET", "/inbox/count", bt) == {"total": 3, "pending": 3}
        request("POST", f'/inbox/{questions[0]["id"]}/ack', at, expected=404)
        reply = request("POST", "/messages", bt,
                        {"to_agent_id": aid, "text": "Respuesta por HTTPS", "in_reply_to": questions[0]["id"]}, 201)
        assert request("GET", "/inbox", at)["messages"][0]["id"] == reply["id"]
        for message in questions:
            request("POST", f'/inbox/{message["id"]}/ack', bt)
        assert request("GET", "/inbox", bt)["messages"] == []
        assert len(request("GET", "/inbox?unread_only=false", bt)["messages"]) == 3
        request("POST", "/messages", at, {"to_agent_id": "a/b", "text": "invalid"}, 422)
        request("GET", f"/inbox?after_id={2**100}", bt, expected=422)
        stats = request("GET", "/stats", at)
        assert stats["agents_online"] >= 2
        print(json.dumps({"result": "PASS", "url": base, "health": health,
                          "checks": "public onboarding URL, shared-key self-registration, privilege separation, agent discovery, send/reply, inbox counts, pagination, private acknowledgements, validation"}))
    finally:
        # Preserve credentials on disk for scoped cleanup or further verification,
        # including if only one registration completed before a network error.
        credentials_file.write_text(json.dumps({"url": base, "agents": agents}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1] == "--cleanup":
        cleanup()
    else:
        main()

