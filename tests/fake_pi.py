"""Protocol fixture only. It never calls a model or reads real credentials."""
import json
import sys


def send(record):
    sys.stdout.buffer.write(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def response(command, data=None, success=True):
    send({"id": command["id"], "type": "response", "command": command["type"],
          "success": success, "data": data or {}, "error": "fixture rejection" if not success else None})


remembered = ""
session_number = 1
for raw in sys.stdin.buffer:
    command = json.loads(raw)
    if command["type"] == "get_state":
        response(command, {"sessionId": f"fixture-{session_number}", "isStreaming": False})
    elif command["type"] == "new_session":
        remembered = ""
        session_number += 1
        response(command, {"cancelled": False})
    elif command["type"] == "abort":
        response(command)
    elif command["type"] == "prompt":
        mode = command["message"]
        answer = "final\u2028answer"
        user_text = mode.rsplit("User task:\n", 1)[-1]
        if user_text.startswith("fixture:set "):
            remembered = user_text.removeprefix("fixture:set ")
            answer = "stored " + remembered
        elif user_text == "fixture:recall":
            answer = remembered or "no previous context"
        if mode.startswith("You improve this harness"):
            context = json.loads(mode.rsplit("\n\n", 1)[1])
            payload = context["target"]["payload"]
            defects = {e["data"]["defect"] for e in context["training_evidence"] if e["kind"] == "feedback"}
            if context["target"]["kind"] == "improver":
                existing = {r["defect"]: r for r in payload["rules"]}
                for defect in defects:
                    existing[defect] = {"defect": defect, "patch": context["allowed_defect_patches"][defect]}
                payload = {"instructions": payload["instructions"] + "\nFixture-specific refinement.",
                           "rules": list(existing.values())}
            else:
                for rule in context["active_improvement_skill"]["rules"]:
                    if rule["defect"] in defects:
                        payload.update(rule["patch"])
            answer = json.dumps({"payload": payload})
        if mode == "handled":
            response(command, {"disposition": "handled"})
            continue
        if mode == "reject":
            response(command, success=False)
            continue
        if mode == "exit":
            sys.exit(2)
        if mode == "malformed":
            sys.stdout.buffer.write(b"not-json\n")
            sys.stdout.buffer.flush()
            continue
        if mode == "stall":
            response(command, {"disposition": "started"})
            continue
        # Send events BEFORE acceptance to test the subscriber race.
        send({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "contentIndex": 0, "delta": "partial\u2028text"}})
        if mode == "retry":
            send({"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                  "errorMessage": "temporary", "content": []}})
            send({"type": "agent_end", "willRetry": True})
        send({"type": "message_end", "message": {"role": "assistant",
              "stopReason": "error" if mode == "error" else "stop",
              "errorMessage": "fixture provider failure" if mode == "error" else None,
              "content": [{"type": "text", "text": answer}]}})
        send({"type": "agent_end", "willRetry": False})
        send({"type": "agent_settled", "aborted": mode == "abort"})
        response(command, {"disposition": "started"})
