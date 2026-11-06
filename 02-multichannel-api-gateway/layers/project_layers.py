"""Lambda layers for the WhatsApp API Gateway integration."""

import hashlib
import subprocess
from pathlib import Path
from typing import Optional

from constructs import Construct
from aws_cdk import (
    aws_lambda as _lambda,
)

LAYERS_DIR = Path(__file__).resolve().parent
BUILD_DIR = LAYERS_DIR / ".build"
HASH_FILE = LAYERS_DIR / ".build.sha256"


def source_hash() -> str:
    """Hash of everything that goes into the layer (sources, requirements, build script)."""
    digest = hashlib.sha256()
    files = sorted((LAYERS_DIR / "common" / "python").glob("*.py"))
    files += [LAYERS_DIR / "requirements.txt", LAYERS_DIR / "build_layer.sh"]
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def ensure_layer_build() -> str:
    """Rebuild `layers/.build` when the sources changed. Needs `uv` and network access."""
    current = source_hash()
    if BUILD_DIR.exists() and HASH_FILE.exists() and HASH_FILE.read_text().strip() == current:
        return str(BUILD_DIR)
    result = subprocess.run(
        ["bash", str(LAYERS_DIR / "build_layer.sh")], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"Layer build failed:\n{result.stdout}\n{result.stderr}")
    HASH_FILE.write_text(current)
    return str(BUILD_DIR)


class ProjectLayers(Construct):
    """Common Lambda layer with shared utilities and pinned dependencies."""

    def __init__(self, scope: Construct, construct_id: str, layer_path: Optional[str] = None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # `layer_path` (context `layer_path`) lets tests and offline runs skip the dependency build.
        self.common_layer = _lambda.LayerVersion(
            self,
            "CommonLayer",
            code=_lambda.Code.from_asset(layer_path or ensure_layer_build()),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_13],
            compatible_architectures=[_lambda.Architecture.ARM_64],
            description="Common utilities and pinned dependencies for WhatsApp API Gateway integration",
        )
