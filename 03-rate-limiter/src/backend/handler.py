"""Backend stand-in: the "real server" behind the rate limiter.

The limiter invokes this Lambda directly (SDK Invoke, signed with the EC2
instance role) only for allowed requests. It does no real work: it echoes the
request so the caller can see it got through, plus the time Lambda spent.

Chat mode (leaky bucket workers): when the event has "sleep_ms", this acts as a
slow chat agent. It sleeps (at most 1 second), then answers with a reply text.
The token bucket never sends "sleep_ms", so its behaviour is unchanged.
"""

# json: build the response body.
import json
# time: measure how long this handler ran, and sleep in chat mode.
import time

# Longest pretend "thinking" time we allow, in ms (keeps a test from tying up the Lambda).
MAX_SLEEP_MS = 1000
# Longest part of the message echoed back in the reply, in characters.
MAX_REPLY_ECHO = 100


def handler(event, context):
    # Start the clock.
    t0 = time.perf_counter()
    # Test hook: a worker sets "fail": true to check retries and the dead-letter queue.
    if event.get("fail") is True:
        # Raising makes the Lambda invocation report a function error to the caller.
        raise RuntimeError("simulated backend failure (fail hook)")
    # The limiter sends {"path": "/orders", "method": "GET"}; keep only those two
    # fields so the response can't echo anything unexpected.
    req = {"path": str(event.get("path", "")), "method": str(event.get("method", ""))}
    # Response the limiter passes straight back to the client.
    response = {
        # Marker that the request reached the backend.
        "ok": True,
        # What we received.
        "request": req,
        # Time spent inside this handler, in ms (tiny; most backend time is Invoke overhead).
        "backend_ms": round((time.perf_counter() - t0) * 1000, 3),
        # Lambda request id, handy for matching with CloudWatch logs.
        "request_id": getattr(context, "aws_request_id", ""),
    }
    # Chat mode only when the caller asked for it (token bucket requests skip all of this).
    if "sleep_ms" in event:
        # Parse the pause defensively: a bad value counts as 0 instead of crashing.
        try:
            # Milliseconds the pretend agent "thinks".
            sleep_ms = float(event.get("sleep_ms") or 0)
        except (TypeError, ValueError):
            # Not a number: no pause.
            sleep_ms = 0.0
        # Clamp to 0..MAX_SLEEP_MS so nobody can make the Lambda sleep for long.
        sleep_ms = max(0.0, min(sleep_ms, MAX_SLEEP_MS))
        # Pretend to think.
        time.sleep(sleep_ms / 1000)
        # The message the user typed (text only, shortened).
        message = str(event.get("message", ""))[:MAX_REPLY_ECHO]
        # The agent's answer.
        response["reply"] = "agent reply to: " + message
        # Time the handler really took, now including the pause.
        response["backend_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    # Hand the response back to the caller.
    return response
