"""Lambda layers for the WhatsApp API Gateway integration."""

from constructs import Construct
from aws_cdk import (
    aws_lambda as _lambda,
)


class ProjectLayers(Construct):
    """Common Lambda layer with shared utilities."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.common_layer = _lambda.LayerVersion(
            self,
            "CommonLayer",
            code=_lambda.Code.from_asset("layers/common"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="Common utilities for WhatsApp API Gateway integration",
        )
