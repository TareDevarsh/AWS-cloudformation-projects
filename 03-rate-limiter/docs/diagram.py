"""Render the architecture diagram with official AWS icons.

    pip install diagrams      # also needs Graphviz: brew install graphviz
    python3 docs/diagram.py   # writes docs/images/architecture.png

Icon catalog: https://diagrams.mingrammer.com/docs/nodes/aws
Shows both algorithms: the token bucket path (limiter -> Redis -> backend)
and the leaky bucket extras (SQS FIFO queues + fixed-rate workers).
One node per role keeps the arrows readable; each role runs in 3 languages.
"""
# Path: build the output file path next to this script.
from pathlib import Path

# Diagram building blocks: canvas, grouping boxes, and labelled arrows.
from diagrams import Cluster, Diagram, Edge
# AWS icons.
from diagrams.aws.compute import Lambda
from diagrams.aws.integration import SQS
from diagrams.aws.storage import S3
# Non-AWS icons: the client, nginx, Redis, and a container for the language servers.
from diagrams.onprem.client import User
from diagrams.onprem.container import Docker
from diagrams.onprem.inmemory import Redis
from diagrams.onprem.network import Nginx

# Output: docs/images/architecture.png (the library adds ".png").
OUT = Path(__file__).parent / "images" / "architecture"
# Make sure docs/images/ exists.
OUT.parent.mkdir(exist_ok=True)

# Graph-wide styling: padding, readable font, room between columns, curved arrows.
GRAPH = {"pad": "0.4", "fontsize": "20", "ranksep": "1.2", "nodesep": "0.8", "splines": "spline"}

# Left-to-right layout, written to OUT, without opening a viewer.
with Diagram("03 · Rate Limiter", filename=str(OUT), show=False, direction="LR", graph_attr=GRAPH):
    # The caller: your laptop, the only IP allowed in.
    client = User("Your laptop\n(only allowed IP)")

    # Everything inside the CloudFormation stack.
    with Cluster("AWS · stack rate-limiter-<stage>"):
        # The single EC2 instance and what Docker Compose runs on it.
        with Cluster("EC2 t3.small · Docker Compose"):
            # nginx: the only published port.
            router = Nginx("nginx router\n:8080 (only open port)")
            # The limiters: same algorithm in Python, Go and Node.
            limiters = Docker("Limiters ×3\nPython · Go · Node")
            # Redis: counters, line counts, jobs. Never published.
            redis = Redis("Redis\ncounters · jobs")
            # Leaky bucket only: workers set the fixed rate.
            workers = Docker("Workers ×3\n(leaky bucket only)")

        # Leaky bucket only: the waiting line, outside the instance.
        queues = SQS("SQS FIFO\n6 queues + 6 DLQs\n(leaky bucket)")
        # Rules file and code zip.
        rules = S3("S3\nrules.yaml + code zip")
        # Backend stand-in for a real server.
        backend = Lambda("Backend Lambda\n(stand-in server)")

    # Request path: laptop -> nginx -> limiter of that language.
    client >> Edge(label="HTTP\n/python /go /node") >> router >> limiters
    # Each limiter does one atomic Lua call in Redis.
    limiters >> Edge(label="atomic Lua", color="darkgreen") >> redis
    # Token bucket: allowed requests go straight to the backend.
    limiters >> Edge(label="allowed\n(token bucket)", color="darkgreen") >> backend
    # Leaky bucket: accepted requests go into the queue...
    limiters >> Edge(label="enqueue\n(leaky bucket)", color="darkorange", style="dashed") >> queues
    # ...workers take them at a fixed rate...
    queues >> Edge(label="fixed rate", color="darkorange", style="dashed") >> workers
    # ...and call the backend.
    workers >> Edge(color="darkorange", style="dashed") >> backend
    # Rules are loaded from S3 and refreshed when they change.
    rules >> Edge(label="re-read when changed", style="dotted") >> limiters
