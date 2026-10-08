"""Connector adapters (TASK-M6-001).

Only adapters here may touch vendor SDKs (boto3); no SDK type crosses the
``connectors.interfaces`` seam. Local folder + web use stdlib/httpx only; git
shells out to the ``git`` CLI (external tool, not vendored code).
"""

from .git_connector import GitConnector
from .local_connector import LocalFolderConnector
from .s3_connector import S3Connector
from .web_connector import WebConnector

__all__ = [
    "GitConnector",
    "LocalFolderConnector",
    "S3Connector",
    "WebConnector",
]
