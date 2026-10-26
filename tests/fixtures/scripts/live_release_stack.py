"""Run the real release journey on an ephemeral GitHub-hosted runner.

No mocks, model stubs or parser overrides. The existing browser script owns
M2/M5 assertions. Only curated JSON leaves this helper: process logs, temporary
credentials, token responses and provider errors remain private and are removed.
One short-answer journey and six public fixtures bound the workload; production
provider timeouts/retries and this helper's process deadlines bound elapsed time.
There is no claim of a hard generation-token or monetary spending cap.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MODEL = "google/gemma-4-26b-a4b-it:free"
PARSERS = {
    "DoclingParser", "PdfTextParser", "PdfTableParser", "DocxParser", "HtmlParser",
    "MarkdownParser", "CsvParser", "TextParser",
}


class GateFailed(RuntimeError):
    """A failed gate whose message contains a fixed non-secret code only."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise GateFailed(code)


def private_json(path: Path, data: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


class Stack:
    def __init__(self, report_dir: Path) -> None:
        self.reports = report_dir
        self.private: Path | None = None
        self.processes: list[subprocess.Popen] = []
        self.env = dict(os.environ)
        self.stage = "configuration"
        self.storage = None
        self.bucket_created = False
        self.suffix = uuid.uuid4().hex
        self.bucket = "live-release-" + self.suffix
        self.collection = "knowledge_test_release_" + self.suffix
        self.summary: dict[str, object] = {
            "passed": False, "failure": None, "ephemeral_stack": True,
            "hard_generation_token_cap": False,
        }

    def command(self, name: str, command: list[str], *, timeout: int) -> None:
        require(self.private is not None, "private_directory_required")
        with (self.private / f"{name}.log").open("wb") as output:
            os.chmod(output.name, 0o600)
            process = subprocess.Popen(
                command, cwd=REPO, env=self.child_environment(name),
                stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes.append(process)
        require(process.wait(timeout=timeout) == 0, f"{name}_failed")

    def start(self, name: str, command: list[str]) -> subprocess.Popen:
        require(self.private is not None, "private_directory_required")
        with (self.private / f"{name}.log").open("wb") as output:
            os.chmod(output.name, 0o600)
            process = subprocess.Popen(
                command, cwd=REPO, env=self.child_environment(name),
                stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes.append(process)
        return process

    def child_environment(self, name: str) -> dict[str, str]:
        env = dict(self.env)
        if name not in {"api", "worker"}:
            env.pop("OPENROUTER_API_KEY", None)
            env.pop("AUTH_JWT_SECRET", None)
        return env

    def configure(self) -> None:
        require(os.environ.get("GITHUB_ACTIONS") == "true", "github_actions_required")
        require(os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "hosted_runner_required")
        require(os.environ.get("LIVE_RELEASE_EPHEMERAL") == "1", "ephemeral_opt_in_required")
        require(bool(os.environ.get("OPENROUTER_API_KEY", "").strip()), "provider_key_required")
        # The fixture connection strings are intentionally public and fixed.
        # Refuse inherited deployment URLs before executing any migration.
        expected = {
            "DATABASE_URL": "postgresql+psycopg://knowledge:knowledge@127.0.0.1:5432/knowledge",
            "REDIS_URL": "redis://127.0.0.1:6379/0", "QDRANT_URL": "http://127.0.0.1:6333",
            "S3_ENDPOINT_URL": "http://127.0.0.1:8333",
            "API_BASE_URL": "http://127.0.0.1:8000", "BASE_URL": "http://127.0.0.1:3001",
        }
        require(all(self.env.get(key) == value for key, value in expected.items()),
                "fixture_urls_required")
        require(self.env.get("ANSWER_MODEL") == MODEL, "gemma_generation_required")
        require(self.env.get("RERANK_ENABLED") == "true", "rerank_required")
        require(self.env.get("APP_ENV") == "ci", "ci_environment_required")
        require(Path(self.env.get("CHROME_PATH", "")).is_file(), "chrome_required")
        self.private = Path(tempfile.mkdtemp(prefix="live-release-private-", dir=os.environ[
            "RUNNER_TEMP"
        ]))
        self.env.update({
            "PYTHONPATH": os.pathsep.join(str(REPO / part) for part in (
                "packages", "apps/api", "apps/worker",
            )),
            "AUTH_JWT_SECRET": secrets.token_urlsafe(48),
            "AUTH_ACCESS_TOKEN_TTL_SECONDS": "3600",
            "VECTOR_COLLECTION": self.collection, "S3_BUCKET": self.bucket,
            "INGEST_QUEUE_PREFIX": f"release-{self.suffix}:",
            "SYNC_QUEUE_PREFIX": f"release-{self.suffix}:",
            "GRAPH_MODE": "off", "SYNC_WORKER_ENABLED": "off",
            "MIGRATIONS_ON_BOOT": "false", "LOG_JSON": "true",
            "API_HOST": "127.0.0.1", "PORT": "3001", "HOSTNAME": "127.0.0.1",
            "RUN_RELEASE_E2E": "1", "RELEASE_TIMEOUT_MS": "180000",
            "PLAYWRIGHT_MODULE": str(Path(os.environ["RUNNER_TEMP"]) / (
                "live-release-qa/node_modules/playwright/index.mjs"
            )),
            "RELEASE_REPORT_FILE": str(self.private / "journey-raw.json"),
            "AGENT_STORE_DB": str(self.private / "agents.sqlite"),
            "AGENT_STORE_SHARED_ROOT": str(self.private),
        })
        require(Path(self.env["PLAYWRIGHT_MODULE"]).is_file(), "playwright_required")
        provider_file = self.private / "cli-provider.json"
        private_json(provider_file, {"OPENROUTER_API_KEY": self.env["OPENROUTER_API_KEY"]})
        self.env["LIVE_EVAL_PROVIDER_KEY_FILE"] = str(provider_file)
        self.env["MULTILINGUAL_REPORT_FILE"] = str(self.private / "multilingual-raw.json")
        require((REPO / "apps/web/.next/standalone/server.js").is_file(),
                "standalone_release_required")

    def wait_backing_services(self) -> None:
        self.stage = "backing_services"
        for port in (5432, 6379, 6333, 8333):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        break
                except OSError:
                    time.sleep(1)
            else:
                raise GateFailed("fixture_service_unavailable")

    def create_bucket(self) -> None:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError

        self.stage = "s3_roundtrip"
        self.storage = boto3.client(
            "s3", endpoint_url=self.env["S3_ENDPOINT_URL"],
            aws_access_key_id=self.env["S3_ACCESS_KEY"],
            aws_secret_access_key=self.env["S3_SECRET_KEY"],
            config=Config(connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1},
                          s3={"addressing_style": "path"}),
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                try:
                    self.storage.create_bucket(Bucket=self.bucket)
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                        raise
                self.bucket_created = True
                self.storage.put_object(
                    Bucket=self.bucket, Key="probe", Body=b"synthetic public QA",
                )
                landed = self.storage.get_object(Bucket=self.bucket, Key="probe")["Body"]
                require(landed.read() == b"synthetic public QA", "s3_roundtrip_mismatch")
                landed.close()
                self.storage.delete_object(Bucket=self.bucket, Key="probe")
                return
            except Exception:
                time.sleep(1)
        raise GateFailed("s3_not_ready")

    def wait_http(self, url: str, process: subprocess.Popen) -> None:
        import httpx

        deadline = time.monotonic() + 120
        with httpx.Client(timeout=3, trust_env=False) as client:
            while time.monotonic() < deadline:
                require(process.poll() is None, "application_exited")
                try:
                    if client.get(url).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(1)
        raise GateFailed("application_not_ready")

    def bootstrap(self) -> None:
        import httpx

        self.stage = "bootstrap_account"
        credentials = {
            "email": f"release-{self.suffix}@example.test",
            "password": secrets.token_urlsafe(32),
        }
        with httpx.Client(base_url=self.env["API_BASE_URL"], timeout=15, trust_env=False) as client:
            require(client.post("/auth/register", json=credentials).status_code == 201,
                    "fixture_registration_failed")
            login = client.post("/auth/login", json=credentials)
            require(login.status_code == 200, "fixture_login_failed")
            token = login.json().get("access_token")
            require(isinstance(token, str) and bool(token), "fixture_token_missing")
            headers = {"Authorization": f"Bearer {token}"}
            workspace = client.post("/v1/workspaces", json={"name": "Synthetic release QA"},
                                    headers=headers)
            require(workspace.status_code == 201, "fixture_workspace_failed")
            workspace_id = str(uuid.UUID(workspace.json()["id"]))
            configured = client.post(
                f"/v1/workspaces/{workspace_id}/model-configs", headers=headers,
                json={"name": "release QA", "preferred_model": MODEL, "sensitive": False},
            )
            require(configured.status_code == 201, "fixture_model_config_failed")
            require(configured.json().get("preferred_model") == MODEL, "fixture_model_mismatch")
        credential_file = self.private / "qa-credentials.json"
        private_json(credential_file, credentials)
        require(credential_file.stat().st_mode & 0o077 == 0, "private_credentials_required")
        self.env.update({"CREDENTIAL_FILE": str(credential_file), "WORKSPACE_ID": workspace_id})
        # Only non-secret pointers/IDs are exported. Passwords, JWTs and the
        # OpenRouter key never enter GITHUB_ENV or an authentication env file.
        with Path(os.environ["GITHUB_ENV"]).open("a") as output:
            for name in ("CREDENTIAL_FILE", "WORKSPACE_ID", "BASE_URL", "API_BASE_URL"):
                output.write(f"{name}={self.env[name]}\n")

    def wait_worker(self, process: subprocess.Popen) -> None:
        from redis import Redis

        self.stage = "worker_start"
        client = Redis.from_url(self.env["REDIS_URL"], socket_timeout=2)
        try:
            deadline = time.monotonic() + 90
            key = f"ingest:{self.env['INGEST_QUEUE_PREFIX']}worker_heartbeat"
            while time.monotonic() < deadline:
                require(process.poll() is None, "worker_exited")
                if client.ttl(key) > 0:
                    return
                time.sleep(1)
            raise GateFailed("worker_heartbeat_missing")
        finally:
            client.close()

    def browser_report(self) -> None:
        """Export only known result fields, including safe browser failure stages."""
        path = self.private / "journey-raw.json"
        if not path.exists():
            return
        raw = json.loads(path.read_text())
        count = raw.get("formats_ready")
        require(type(count) is int and 0 <= count <= 6, "invalid_browser_report")
        modes = raw.get("search_modes")
        require(isinstance(modes, list) and all(
            mode in {"hybrid", "semantic", "keyword"} for mode in modes
        ), "invalid_browser_report")
        failure = raw.get("failure")
        if failure is not None:
            require(isinstance(failure, dict), "invalid_browser_report")
            require(all(isinstance(failure.get(key), str) and re.fullmatch(
                r"[A-Za-z0-9_.-]{1,100}", failure[key],
            ) for key in ("stage", "error_type")), "invalid_browser_report")
            failure = {key: failure[key] for key in ("stage", "error_type")}
            code = raw["failure"].get("code")
            if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,100}", code):
                failure["code"] = code
        answer_fields = {}
        for key in ("answer_status", "publication_status", "answer_reason_code"):
            value = raw.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z_0-9]{1,64}", value):
                answer_fields[key] = value
        status = raw.get("answer_http_status")
        if type(status) is int and 100 <= status <= 599:
            answer_fields["answer_http_status"] = status
        observation = raw.get("answer_observation")
        if isinstance(observation, dict):
            require(observation.get("transport") == "actual_browser_fetch_clone"
                    and observation.get("request_count") == 1
                    and type(observation.get("bytes")) is int
                    and 0 <= observation["bytes"] <= 2 * 1024 * 1024,
                    "invalid_browser_observation")
            answer_fields["answer_observation"] = {
                key: observation[key] for key in ("transport", "request_count", "bytes")
            }
        private_json(self.reports / "journey.json", {
            "formats_ready": count, "citation_passage": raw.get("citation_passage") is True,
            "keyboard": raw.get("keyboard") is True, "search_modes": modes, "failure": failure,
            **answer_fields,
        })

    def evidence(self) -> None:
        from qdrant_client import QdrantClient

        self.stage = "acceptance_evidence"
        raw = json.loads((self.private / "journey-raw.json").read_text())
        require(raw.get("failure") is None, "browser_report_failed")
        require(raw.get("formats_ready") == 6 and raw.get("citation_passage") is True
                and raw.get("keyboard") is True, "browser_assertions_missing")
        require(raw.get("search_modes") == ["hybrid", "semantic", "keyword"],
                "search_assertions_missing")
        parsed = []
        for line in (self.private / "worker.log").read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("event") != "ingest_job_ready":
                continue
            disclosure = event.get("parse", {})
            primary, used = disclosure.get("primary"), disclosure.get("used")
            require(primary in PARSERS and used in PARSERS, "parser_evidence_invalid")
            parsed.append({"primary": primary, "used": used,
                           "fallback_used": bool(disclosure.get("fallback_used"))})
        require(len(parsed) == 6, "six_real_parser_outcomes_required")
        self.summary["parser_outcomes"] = parsed
        client = QdrantClient(url=self.env["QDRANT_URL"], timeout=5)
        try:
            info = client.get_collection(self.collection)
            require(info.config.params.vectors["dense"].size == 3072,
                    "actual_embedding_dimension_mismatch")
            require(info.points_count == 0, "browser_vector_cleanup_incomplete")
            self.summary["embedding_dimension"] = 3072
            self.summary["vector_cleanup_empty"] = True
        finally:
            client.close()

    def run(self) -> None:
        self.configure()
        self.wait_backing_services()
        self.create_bucket()
        self.stage = "alembic_migrations"
        self.command("migrations", [
            sys.executable, "-c", "from app.db import run_migrations; run_migrations()",
        ], timeout=180)
        self.stage = "api_start"
        api = self.start("api", [sys.executable, "-m", "uvicorn", "app.main:app",
                                 "--host", "127.0.0.1", "--port", "8000", "--no-access-log"])
        self.wait_http(self.env["API_BASE_URL"] + "/health", api)
        self.wait_worker(self.start("worker", [sys.executable, "-m", "worker.main"]))
        self.bootstrap()
        self.stage = "web_start"
        web = self.start("web", ["node", str(REPO / "apps/web/.next/standalone/server.js")])
        self.wait_http(self.env["BASE_URL"] + "/login", web)
        self.stage = "browser_journey"
        try:
            self.command("browser", [
                "bash", str(REPO / "tests/fixtures/scripts/release_journey.sh"),
            ], timeout=1200)
        finally:
            self.browser_report()
        self.evidence()
        self.stage = "multilingual_debugger_parity"
        try:
            self.command("multilingual", ["node", str(
                REPO / "tests/fixtures/scripts/live_multilingual_eval.mjs"
            )], timeout=1200)
        finally:
            multilingual_path = self.private / "multilingual-raw.json"
            if multilingual_path.is_file():
                result = json.loads(multilingual_path.read_text())
                # The gate publishes only IDs, scores, measurements and fixed
                # statuses. Public fixture answers remain in its private manifest.
                fields = ("run_id", "corpus", "datasets", "scoring", "outcomes",
                          "failures", "trace_checks", "answer_observations", "cli", "passed")
                private_json(self.reports / "multilingual.json", {
                    field: result[field] for field in fields if field in result
                })
        self.summary["passed"] = True

    def cleanup(self) -> None:
        # These are exclusively this helper's session leaders. Never inspect
        # or stop another process, service, bucket or collection by prefix.
        errors = []
        for process in reversed(self.processes):
            try:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except Exception as exc:
                errors.append(type(exc).__name__)
        try:
            if self.storage is not None:
                try:
                    if self.bucket_created:
                        paginator = self.storage.get_paginator("list_objects_v2")
                        for page in paginator.paginate(Bucket=self.bucket):
                            for obj in page.get("Contents", []):
                                self.storage.delete_object(Bucket=self.bucket, Key=obj["Key"])
                        self.storage.delete_bucket(Bucket=self.bucket)
                finally:
                    self.storage.close()
        except Exception as exc:
            errors.append(type(exc).__name__)
        try:
            if self.private is not None:
                from qdrant_client import QdrantClient

                client = QdrantClient(url=self.env["QDRANT_URL"], timeout=5)
                try:
                    if client.collection_exists(self.collection):
                        client.delete_collection(self.collection)
                finally:
                    client.close()
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            # Credentials/private logs are erased even if service cleanup fails.
            try:
                if self.private is not None:
                    shutil.rmtree(self.private)
            except Exception as exc:
                errors.append(type(exc).__name__)
        require(not errors, "fixture_cleanup_failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.report_dir.mkdir(parents=True, exist_ok=False)
    stack = Stack(args.report_dir)
    try:
        stack.run()
    except Exception as exc:
        stack.summary["failure"] = {
            "stage": stack.stage, "error_type": type(exc).__name__,
            "code": str(exc) if isinstance(exc, GateFailed) else "operation_failed",
        }
    finally:
        try:
            stack.cleanup()
        except Exception as exc:
            stack.summary["passed"] = False
            stack.summary["cleanup_error_type"] = type(exc).__name__
        private_json(args.report_dir / "stack.json", stack.summary)
    # Fixed statuses/types only: never print exception messages, HTTP response
    # bodies, subprocess call logs, credentials, model claims or environment.
    passed = stack.summary["passed"] is True
    print("LIVE_RELEASE_STACK_OK" if passed else "LIVE_RELEASE_STACK_FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        status = main()
    except Exception:
        # Includes report-directory/write failures outside the stack lifecycle.
        # A top-level traceback must never disclose inherited provider context.
        print("LIVE_RELEASE_STACK_FAIL")
        status = 1
    sys.exit(status)
