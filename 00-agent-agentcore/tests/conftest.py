import os

import pytest

# Environment the agent modules read at import time. Set before any import.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TL_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123456789012:secret:test")
os.environ.setdefault("S3_BUCKET", "agent-bucket")
os.environ.pop("BEDROCK_AGENTCORE_MEMORY_ID", None)


@pytest.fixture
def stub_zip(tmp_path):
    """A placeholder deployment package so synth never needs uv or network access."""
    path = tmp_path / "deployment_package.zip"
    path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    return str(path)
