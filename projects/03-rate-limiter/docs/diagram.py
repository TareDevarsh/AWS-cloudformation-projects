"""Render the architecture diagram with official AWS icons.

    pip install diagrams      # also needs Graphviz: brew install graphviz
    python3 docs/diagram.py   # writes docs/images/architecture.png

Icon catalog: https://diagrams.mingrammer.com/docs/nodes/aws
Replace the placeholder nodes below with this project's real components.
"""
from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.network import APIGateway
from diagrams.onprem.client import User

OUT = Path(__file__).parent / "images" / "architecture"
OUT.parent.mkdir(exist_ok=True)

with Diagram("03 · Rate Limiter", filename=str(OUT), show=False,
             direction="LR", graph_attr={"pad": "0.4", "fontsize": "20"}):
    client = User("Client\n(SigV4)")
    with Cluster("AWS · stack rate-limiter-<stage>"):
        api = APIGateway("API\nAWS_IAM auth")
        fn = Lambda("Function")
        store = Dynamodb("Table")
    client >> Edge(label="HTTPS") >> api >> fn >> store
