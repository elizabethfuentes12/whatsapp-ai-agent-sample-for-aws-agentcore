"""Utility to read SSM Parameter Store values at CDK synthesis time."""

import os
import boto3

ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", os.environ.get("CDK_DEFAULT_REGION")))


def get_string_param(parameter_name: str) -> str:
    """Read a string parameter from SSM Parameter Store.

    Args:
        parameter_name: Full SSM parameter path (e.g., /agentcore/agent_runtime_arn).

    Returns:
        Parameter value or empty string if not found.
    """
    try:
        response = ssm.get_parameter(Name=parameter_name)
        parameter = response.get("Parameter")
        if parameter:
            return parameter.get("Value", "")
    except ssm.exceptions.ParameterNotFound:
        print(f"SSM parameter not found: {parameter_name}")
    except Exception as e:
        print(f"Error reading SSM parameter {parameter_name}: {e}")
    return ""
