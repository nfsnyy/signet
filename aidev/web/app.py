import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator

from ..collaboration import collab_answer, collab_send, collaboration_overview
from ..domain import validate_actor_alias
from ..errors import PlatformError
from ..lifecycle import adjudicate_task, close_task, reject_task, verify_task, void_task
from ..service import block, claim, enhancement, init_repository, init_task, status, submit, synchronize, task_log
from ..specs import freeze_task, refreeze_task
from ..taskfile import read_task
from ..vetoes import decide_veto, list_vetoes, propose_veto, settle_expired_vetoes

ACTOR_FIELDS = ("actor", "on_behalf_of", "h1_actor", "h2_actor", "verifier", "confirmed_by")


def check_actor_aliases(document):
    for field in ACTOR_FIELDS:
        value = document.get(field)
        if value is not None:
            validate_actor_alias(value)


class ActorValidatingModel(BaseModel):
    @model_validator(mode="after")
    def _check_actor_aliases(self):
        check_actor_aliases(self.model_dump())
        return self


class ClaimBody(ActorValidatingModel):
    workspace: str
    actor: str
    on_behalf_of: str


class EnhancementBody(ActorValidatingModel):
    workspace: str
    actor: str
    on_behalf_of: str
    title: str
    details: str


class CollabSendBody(ActorValidatingModel):
    workspace: str
    actor: str
    on_behalf_of: str
    summary: str
    type: str | None = None
    message_type: str | None = None
    to_task: str | None = None
    target_task: str | None = None
    details: str | None = None
    body: str | None = None


class CollabAnswerBody(ActorValidatingModel):
    workspace: str
    actor: str
    on_behalf_of: str
    action: str
    note: str | None = None


class RepositoryBody(ActorValidatingModel):
    repo: str
    actor: str
    name: str | None = None
    owner: str | None = None


class InitTaskBody(ActorValidatingModel):
    repo: str
    workspace: str
    title: str
    requirement: str
    owner: str
    actor: str


class SyncBody(ActorValidatingModel):
    workspace: list
    actor: str
    repo: str | None = None
    push: bool = False


class RejectBody(ActorValidatingModel):
    category: str
    reason: str
    actor: str
    repo: str | None = None
    workspace: str | None = None
    push: bool = False


class CloseBody(ActorValidatingModel):
    decision: str
    actor: str
    repo: str | None = None
    reason: str | None = None
    push: bool = False


class VoidBody(ActorValidatingModel):
    reason: str
    actor: str
    repo: str | None = None
    push: bool = False


class AdjudicateBody(ActorValidatingModel):
    category: str
    reason: str
    actor: str
    repo: str | None = None
    workspace: str | None = None
    push: bool = False


def save_upload(upload, directory, fallback_name):
    if upload is None:
        return None
    filename = Path(upload.filename or fallback_name).name or fallback_name
    target = Path(directory) / filename
    target.write_bytes(upload.file.read())
    return str(target)


def materialize_assets(uploads, manifest, temporary):
    try:
        relative_paths = json.loads(manifest or "[]")
    except json.JSONDecodeError:
        raise PlatformError("c3.invalid_fixture", "asset manifest is not valid JSON")
    if not isinstance(relative_paths, list):
        raise PlatformError("c3.invalid_fixture", "asset manifest must be a list")
    uploads = uploads or []
    if len(uploads) != len(relative_paths):
        raise PlatformError("c3.invalid_fixture", "uploaded assets do not match the contract asset manifest")
    for relative, upload in zip(relative_paths, uploads):
        if not isinstance(relative, str) or not relative.strip():
            raise PlatformError("c3.invalid_fixture", "asset path must be a non-empty relative string")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise PlatformError("c3.invalid_asset_path", f"asset escapes the spec directory: {relative}")
        target = Path(temporary) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(upload.file.read())


def create_app(default_repo=None):
    app = FastAPI(title="signet 平台服务", version="0.2.0")

    def resolve_repo(request_repo):
        repo = request_repo or default_repo or os.environ.get("SIGNET_PLATFORM_REPO")
        if not repo:
            raise PlatformError(
                "git.not_repository",
                "未指定状态仓库：请在请求中提供 repo，或服务端以 SIGNET_PLATFORM_REPO 启动",
            )
        return repo

    def operator_repo(request_repo):
        repository = resolve_repo(request_repo)
        settle_expired_vetoes(repository)
        return repository

    def workspace_task_id(workspace, task_id):
        metadata, _ = read_task(Path(workspace) / "task.md")
        if metadata.get("task_id") != task_id:
            raise PlatformError("task.not_found", f"工作区不属于任务 {task_id}")
        return metadata

    @app.exception_handler(PlatformError)
    async def handle_platform_error(request, exc):
        status_code = 409 if exc.exit_code == 3 else 400
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request, exc):
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal.error", "message": str(exc), "details": {"type": type(exc).__name__}}},
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/tasks/{task_id}/status")
    def status_endpoint(task_id: str, workspace: str = Query(...)):
        workspace_task_id(workspace, task_id)
        return status(workspace)

    @app.get("/api/tasks/{task_id}/events")
    def events_endpoint(
        task_id: str,
        repo: str | None = None,
        role: str = Header(default="operator", alias="X-Signet-Role"),
    ):
        repository = resolve_repo(repo)
        if role == "operator":
            settle_expired_vetoes(repository)
        return {"events": task_log(repository, task_id)}

    @app.post("/api/tasks/{task_id}/claims")
    def claims_endpoint(task_id: str, body: ClaimBody):
        workspace_task_id(body.workspace, task_id)
        return claim(body.workspace, body.actor, body.on_behalf_of)

    @app.post("/api/tasks/{task_id}/submission")
    def submission_endpoint(
        task_id: str,
        workspace: str = Form(...),
        actor: str = Form(...),
        on_behalf_of: str = Form(...),
        report: UploadFile = File(...),
        evidence: list[UploadFile] = File(...),
    ):
        check_actor_aliases({"actor": actor, "on_behalf_of": on_behalf_of})
        workspace_task_id(workspace, task_id)
        with tempfile.TemporaryDirectory(prefix="aidev-web-submit-") as temporary:
            report_path = save_upload(report, temporary, "report.json")
            evidence_paths = [save_upload(item, temporary, "evidence") for item in evidence]
            return submit(workspace, actor, on_behalf_of, report_path, evidence_paths)

    @app.post("/api/tasks/{task_id}/blocks")
    def blocks_endpoint(
        task_id: str,
        workspace: str = Form(...),
        actor: str = Form(...),
        on_behalf_of: str = Form(...),
        reason: str = Form(...),
        evidence: list[UploadFile] = File(...),
    ):
        check_actor_aliases({"actor": actor, "on_behalf_of": on_behalf_of})
        workspace_task_id(workspace, task_id)
        with tempfile.TemporaryDirectory(prefix="aidev-web-block-") as temporary:
            evidence_paths = [save_upload(item, temporary, "evidence") for item in evidence]
            return block(workspace, actor, on_behalf_of, reason, evidence_paths)

    @app.post("/api/tasks/{task_id}/enhancements")
    def enhancements_endpoint(task_id: str, body: EnhancementBody):
        workspace_task_id(body.workspace, task_id)
        return enhancement(body.workspace, body.actor, body.on_behalf_of, body.title, body.details)

    @app.post("/api/tasks/{task_id}/collaborations")
    def collaborations_endpoint(task_id: str, body: CollabSendBody):
        workspace_task_id(body.workspace, task_id)
        message_type = body.type if body.type is not None else body.message_type
        to_task = body.to_task if body.to_task is not None else body.target_task
        details = body.details if body.details is not None else body.body
        return collab_send(body.workspace, body.actor, body.on_behalf_of, message_type, to_task, body.summary, details)

    @app.post("/api/collaborations/{message_id}/answers")
    def collab_answers_endpoint(message_id: str, body: CollabAnswerBody):
        return collab_answer(body.workspace, body.actor, body.on_behalf_of, message_id, body.action, body.note)

    @app.post("/api/repositories")
    def repositories_endpoint(body: RepositoryBody):
        return init_repository(body.repo, body.actor, body.name, body.owner)

    @app.post("/api/tasks")
    def init_task_endpoint(body: InitTaskBody):
        return init_task(body.repo, body.workspace, body.title, body.requirement, body.owner, body.actor)

    @app.post("/api/tasks/{task_id}/freeze")
    def freeze_endpoint(
        task_id: str,
        workspace: str = Form(...),
        h1_actor: str = Form(...),
        h2_actor: str = Form(...),
        card: UploadFile = File(...),
        contract: UploadFile = File(...),
        mapping: UploadFile = File(...),
        repo: str | None = Form(None),
        operation_source: UploadFile | None = File(None),
        push: bool = Form(False),
        asset_manifest: str = Form("[]"),
        assets: list[UploadFile] | None = File(None),
    ):
        check_actor_aliases({"h1_actor": h1_actor, "h2_actor": h2_actor})
        repository = operator_repo(repo)
        with tempfile.TemporaryDirectory(prefix="aidev-web-freeze-") as temporary:
            card_path = save_upload(card, temporary, "acceptance-card.json")
            contract_path = save_upload(contract, temporary, "contract.json")
            mapping_path = save_upload(mapping, temporary, "mapping.json")
            operation_source_path = save_upload(operation_source, temporary, "operations.json")
            materialize_assets(assets, asset_manifest, temporary)
            return freeze_task(repository, task_id, workspace, card_path, contract_path, mapping_path, operation_source_path, h1_actor, h2_actor, push)

    @app.post("/api/tasks/{task_id}/refreeze")
    def refreeze_endpoint(
        task_id: str,
        workspace: str = Form(...),
        h1_actor: str = Form(...),
        h2_actor: str = Form(...),
        reason: str = Form(...),
        card: UploadFile = File(...),
        contract: UploadFile = File(...),
        mapping: UploadFile = File(...),
        repo: str | None = Form(None),
        operation_source: UploadFile | None = File(None),
        push: bool = Form(False),
        asset_manifest: str = Form("[]"),
        assets: list[UploadFile] | None = File(None),
    ):
        check_actor_aliases({"h1_actor": h1_actor, "h2_actor": h2_actor})
        repository = operator_repo(repo)
        with tempfile.TemporaryDirectory(prefix="aidev-web-refreeze-") as temporary:
            card_path = save_upload(card, temporary, "acceptance-card.json")
            contract_path = save_upload(contract, temporary, "contract.json")
            mapping_path = save_upload(mapping, temporary, "mapping.json")
            operation_source_path = save_upload(operation_source, temporary, "operations.json")
            materialize_assets(assets, asset_manifest, temporary)
            return refreeze_task(repository, task_id, workspace, card_path, contract_path, mapping_path, operation_source_path, h1_actor, h2_actor, reason, push)

    @app.post("/api/sync")
    def sync_endpoint(body: SyncBody):
        repository = operator_repo(body.repo)
        return synchronize(repository, body.workspace, body.actor, body.push)

    @app.post("/api/tasks/{task_id}/verify")
    def verify_endpoint(
        task_id: str,
        verifier: str = Form(...),
        confirmed_by: str = Form(...),
        repo: str | None = Form(None),
        execute: bool = Form(False),
        push: bool = Form(False),
        results: UploadFile | None = File(None),
        manual_results: UploadFile | None = File(None),
    ):
        check_actor_aliases({"verifier": verifier, "confirmed_by": confirmed_by})
        repository = operator_repo(repo)
        with tempfile.TemporaryDirectory(prefix="aidev-web-verify-") as temporary:
            results_path = save_upload(results, temporary, "results.json")
            manual_results_path = save_upload(manual_results, temporary, "manual-results.json")
            return verify_task(repository, task_id, verifier, confirmed_by, results_path, execute, manual_results_path, push)

    @app.post("/api/tasks/{task_id}/reject")
    def reject_endpoint(task_id: str, body: RejectBody):
        repository = operator_repo(body.repo)
        return reject_task(repository, task_id, body.category, body.reason, body.actor, body.workspace, body.push)

    @app.post("/api/tasks/{task_id}/close")
    def close_endpoint(task_id: str, body: CloseBody):
        repository = operator_repo(body.repo)
        return close_task(repository, task_id, body.decision, body.actor, body.reason, body.push)

    @app.post("/api/tasks/{task_id}/void")
    def void_endpoint(task_id: str, body: VoidBody):
        repository = operator_repo(body.repo)
        return void_task(repository, task_id, body.reason, body.actor, body.push)

    @app.post("/api/tasks/{task_id}/adjudication")
    def adjudication_endpoint(task_id: str, body: AdjudicateBody):
        repository = operator_repo(body.repo)
        return adjudicate_task(repository, task_id, body.category, body.reason, body.actor, body.workspace, body.push)

    @app.post("/api/vetoes")
    def vetoes_endpoint(
        actor: str = Form(...),
        repo: str | None = Form(None),
        task_id: str | None = Form(None),
        veto_id: str | None = Form(None),
        reason: str | None = Form(None),
        propose: bool = Form(False),
        approve: bool = Form(False),
        reject: bool = Form(False),
        withdraw: bool = Form(False),
        list_flag: bool = Form(False),
        push: bool = Form(False),
        card: UploadFile | None = File(None),
        contract: UploadFile | None = File(None),
        mapping: UploadFile | None = File(None),
        asset_manifest: str = Form("[]"),
        assets: list[UploadFile] | None = File(None),
    ):
        check_actor_aliases({"actor": actor})
        repository = operator_repo(repo)
        actions = [
            name
            for name, flag in (
                ("propose", propose),
                ("approve", approve),
                ("reject", reject),
                ("withdraw", withdraw),
                ("list", list_flag),
            )
            if flag
        ]
        if len(actions) != 1:
            raise PlatformError("veto.invalid_action", "provide exactly one of --propose --approve --reject --withdraw --list")
        with tempfile.TemporaryDirectory(prefix="aidev-web-veto-") as temporary:
            card_path = save_upload(card, temporary, "acceptance-card.json")
            contract_path = save_upload(contract, temporary, "contract.json")
            mapping_path = save_upload(mapping, temporary, "mapping.json")
            materialize_assets(assets, asset_manifest, temporary)
            if list_flag:
                return {"vetoes": list_vetoes(repository)}
            if propose:
                if not task_id or not card_path or not contract_path or not mapping_path:
                    raise PlatformError("veto.missing_arguments", "--task-id --acceptance-card --contract --mapping are required with --propose")
                return propose_veto(repository, task_id, card_path, contract_path, mapping_path, reason, actor, push)
            if not veto_id:
                raise PlatformError("veto.missing_arguments", "--veto-id is required")
            if approve:
                return decide_veto(repository, veto_id, "approve", actor, None, push)
            if reject:
                return decide_veto(repository, veto_id, "reject", actor, reason, push)
            return decide_veto(repository, veto_id, "withdraw", actor, None, push)

    @app.get("/api/inbox")
    def inbox_endpoint(repo: str | None = None, task_id: str | None = None):
        repository = operator_repo(repo)
        return collaboration_overview(repository, task_id)

    return app
