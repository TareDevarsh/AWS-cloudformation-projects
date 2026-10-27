"""Render the architecture diagram with official AWS icons.

    pip install diagrams      # also needs Graphviz: brew install graphviz
    python3 docs/diagram.py   # writes docs/images/architecture.png
"""
from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.management import Cloudwatch
from diagrams.aws.network import APIGateway
from diagrams.aws.security import IAM
from diagrams.onprem.client import User

OUT = Path(__file__).parent / "images" / "architecture"
OUT.parent.mkdir(exist_ok=True)

with Diagram("01 · Key-Value Store", filename=str(OUT), show=False,
             direction="LR", graph_attr={"pad": "0.4", "fontsize": "20"}):
    client = User("Client\n(SigV4)")
    policy = IAM("KvClientPolicy\nexecute-api:Invoke")

    with Cluster("AWS · stack kv-store-<stage>"):
        api = APIGateway("REST API\nAWS_IAM auth")
        fn = Lambda("kv-store\nPython 3.12")
        table = Dynamodb("KvTable\non-demand · TTL")
        logs = Cloudwatch("Logs\n14 days")

    policy >> Edge(style="dashed", label="grants") >> client
    client >> Edge(label="HTTPS") >> api >> Edge(label="proxy") >> fn
    fn >> Edge(label="Get/Put/Delete/Scan") >> table
    fn >> Edge(style="dotted") >> logs
