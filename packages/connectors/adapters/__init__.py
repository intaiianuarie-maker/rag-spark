"""Connector adapters (TASK-M6-001).

Only adapters here may touch vendor SDKs (boto3); no SDK type crosses the
``connectors.interfaces`` seam. Local folder uses stdlib only.
"""

from .local_connector import LocalFolderConnector
from .s3_connector import S3Connector

__all__ = ["LocalFolderConnector", "S3Connector"]
