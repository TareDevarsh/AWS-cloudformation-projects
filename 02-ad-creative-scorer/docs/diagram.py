"""Render the architecture diagram with official AWS icons.
    pip install diagrams      # also needs Graphviz: brew install graphviz
    python3 docs/diagram.py   # writes docs/images/architecture.png
"""
from pathlib import Path
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.integration import Eventbridge, SNS, StepFunctions
from diagrams.aws.management import Cloudwatch
from diagrams.aws.ml import Rekognition, Sagemaker
from diagrams.aws.network import APIGateway
from diagrams.aws.security import IAM, KMS
from diagrams.aws.storage import S3
from diagrams.onprem.client import User

OUT = Path(__file__).parent / "images" / "architecture"
OUT.parent.mkdir(exist_ok=True)

with Diagram("02 · Ad Creative Scorer", filename=str(OUT), show=False,
             direction="LR", graph_attr={"pad": "0.4", "fontsize": "20"}):
    client = User("Client\n(SigV4)")
    policy = IAM("AdScorerClientPolicy\nexecute-api:Invoke")

    with Cluster("AWS · 6 stacks ad-scorer-<env>-*"):
        with Cluster("01 foundation"):
            creatives = S3("creative-assets\nuploads/")
            artifacts = S3("ml-artifacts\ntraining · models · features")
            key = KMS("ScorerKMSKey")

        with Cluster("03 api"):
            api = APIGateway("REST API\nAWS_IAM auth")

        with Cluster("02 compute"):
            presign = Lambda("presigned-url")
            trigger = Lambda("pipeline-trigger")
            get_score = Lambda("get-score")
            extract = Lambda("feature-extraction")
            infer = Lambda("inference")
            table = Dynamodb("scores table\n+ CampaignScoreIndex")

        with Cluster("04 orchestration"):
            sfn = StepFunctions("pipeline\nEXPRESS")
            events = Eventbridge("default bus\nCreativeScoringComplete")

        rekognition = Rekognition("Rekognition")

        with Cluster("05 ml"):
            endpoint = Sagemaker("XGBoost\nscoring-endpoint")

        with Cluster("06 monitoring"):
            alarms = Cloudwatch("7 alarms\n+ dashboard")
            alerts = SNS("alerts topic\n(email)")

    policy >> Edge(style="dashed", label="grants") >> client
    client >> Edge(label="HTTPS") >> api
    client >> Edge(label="PUT presigned URL") >> creatives
    api >> Edge(label="POST /v1/upload") >> presign
    api >> Edge(label="POST /v1/score") >> trigger
    api >> Edge(label="GET /v1/score/{id}") >> get_score
    trigger >> Edge(label="PutItem PENDING") >> table
    trigger >> Edge(label="StartExecution") >> sfn
    get_score >> Edge(label="Query") >> table
    sfn >> extract
    sfn >> infer
    sfn >> Edge(label="FLAGGED / FAILED") >> table
    sfn >> events
    extract >> rekognition
    extract >> Edge(label="features/*.json") >> artifacts
    rekognition >> Edge(style="dotted", label="reads object") >> creatives
    infer >> endpoint
    infer >> Edge(label="SCORED") >> table
    artifacts >> Edge(style="dotted", label="model.tar.gz") >> endpoint
    alarms >> alerts
    key >> Edge(style="dotted", label="encrypts") >> table
