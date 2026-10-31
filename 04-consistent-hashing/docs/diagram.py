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
from diagrams.aws.integration import Eventbridge
from diagrams.aws.network import APIGateway
from diagrams.aws.security import IAM
from diagrams.onprem.client import User

OUT = Path(__file__).parent / "images" / "architecture"
OUT.parent.mkdir(exist_ok=True)

with Diagram("04 · Consistent Hashing", filename=str(OUT), show=False,
             direction="LR", graph_attr={"pad": "0.4", "fontsize": "20", "splines": "spline"}):
    client = User("Client\n(SigV4)")
    with Cluster("AWS · stack consistent-hashing-<stage>"):
        policy = IAM("Client policy\nexecute-api:Invoke")
        api = APIGateway("REST API\nAWS_IAM auth\n/servers · /route · /kv\n/repair · /merkle")
        ring_fn = Lambda("ring\nSHA-1 + binary search\nup / down")
        kv_fn = Lambda("kv coordinator\nN replicas · W/R quorum\nMerkle repair")
        schedule = Eventbridge("Scheduled rule\nbackground repair")
        ring_table = Dynamodb("Ring table\nservers + vnodes + status")
        data_table = Dynamodb("Data table\none item per replica")

    client >> Edge(style="dashed", label="attached") >> policy
    client >> Edge(label="HTTPS") >> api
    api >> Edge(label="/servers · /route") >> ring_fn >> Edge(label="Query / BatchWrite") >> ring_table
    api >> Edge(label="/kv · /repair · /merkle") >> kv_fn
    schedule >> Edge(label="scheduled-repair") >> kv_fn
    kv_fn >> Edge(style="dashed", label="Query ring") >> ring_table
    kv_fn >> Edge(label="parallel Get / Put · Query") >> data_table
