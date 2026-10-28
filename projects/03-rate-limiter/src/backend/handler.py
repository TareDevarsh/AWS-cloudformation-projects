"""Backend stand-in: the "real server" behind the rate limiter.

The limiter invokes this Lambda directly (SDK Invoke, signed with the EC2
instance role) only for allowed requests. It does no real work: it echoes the
request so the caller can see it got through, plus the time Lambda spent.
"""

# json: build the response body.
import json
# time: measure how long this handler ran.
import time


def handler(event, context):
    # Start the clock.
    t0 = time.perf_counter()
    # The limiter sends {"path": "/orders", "method": "GET"}; keep only those two
    # fields so the response can't echo anything unexpected.
    req = {"path": str(event.get("path", "")), "method": str(event.get("method", ""))}
    # Response the limiter passes straight back to the client.
    return {
        # Marker that the request reached the backend.
        "ok": True,
        # What we received.
        "request": req,
        # Time spent inside this handler, in ms (tiny; most backend time is Invoke overhead).
        "backend_ms": round((time.perf_counter() - t0) * 1000, 3),
        # Lambda request id, handy for matching with CloudWatch logs.
        "request_id": getattr(context, "aws_request_id", ""),
    }
