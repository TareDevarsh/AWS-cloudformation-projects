"""Render the architecture diagram with official AWS icons.
    pip install diagrams      # also needs Graphviz: brew install graphviz
    python3 docs/diagram.py   # writes docs/images/architecture.png
Icon catalog: https://diagrams.mingrammer.com/docs/nodes/aws
The concept ring images come from diagram_ring.py and diagram_kv.py (matplotlib).
"""
from pathlib import Path
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.network import APIGateway
from diagrams.aws.security import IAM
from diagrams.onprem.client import User

OUT = Path(__file__).parent / "images" / "architecture"
OUT.parent.mkdir(exist_ok=True)

with Diagram("04 · Consistent Hashing", filename=str(OUT), show=False,
             direction="LR", graph_attr={"pad": "0.4", "fontsize": "20"}):
    client = User("Client\n(SigV4)")
    with Cluster("AWS · stack consistent-hashing-<stage>"):
        policy = IAM("Client policy\nexecute-api:Invoke")
        api = APIGateway("REST API\nAWS_IAM auth\n/servers · /route/{key}")
        fn = Lambda("ring\nSHA-1 + binary search")
        table = Dynamodb("Ring table\nservers + vnodes")
    client >> Edge(style="dashed", label="attached") >> policy
    client >> Edge(label="HTTPS") >> api >> fn >> Edge(label="Query / BatchWrite") >> table
