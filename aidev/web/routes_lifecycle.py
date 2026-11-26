import json
import tempfile
from pathlib import Path

from fastapi import File, Form, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from ..collaboration import collaboration_overview
from ..errors import PlatformError
from ..lifecycle import (
    adjudicate_task,
    close_task,
    reject_task,
    ship_task,
    verify_task,
    void_task,
)
from ..service import init_repository, synchronize_uploaded
from ..specs import freeze_task, refreeze_task
from ..vetoes import decide_veto, list_vetoes, propose_veto
from .authnz import ok
from .context import operator_repo, request_actor, resolve_repo
from .models import (
    AdjudicateBody,
    CloseBody,
    RejectBody,
    RepositoryBody,
    ShipBody,
    VoidBody,
    check_actor_aliases,
)
from .uploads import materialize_assets, save_upload, verify_upload_digests


def register(app):
    app.post("/api/repositories")(repositories_endpoint)
    app.post("/api/tasks/{task_id}/freeze")(freeze_endpoint)
    app.post("/api/tasks/{task_id}/refreeze")(refreeze_endpoint)
    app.post("/api/sync")(sync_endpoint)
    app.post("/api/tasks/{task_id}/verify")(verify_endpoint)
    app.post("/api/tasks/{task_id}/reject")(reject_endpoint)
    app.post("/api/tasks/{task_id}/close")(close_endpoint)
    app.post("/api/tasks/{task_id}/ship")(ship_endpoint)
    app.post("/api/tasks/{task_id}/void")(void_endpoint)
    app.post("/api/tasks/{task_id}/adjudication")(adjudication_endpoint)
    app.post("/api/vetoes")(vetoes_endpoint)
    app.get("/api/inbox")(inbox_endpoint)


def repositories_endpoint(request: Request, body: RepositoryBody):
    app_ctx = request.app.state.app_ctx
    return ok(init_repository(resolve_repo(app_ctx, body.repo), request_actor(request), body.name, body.owner))


def freeze_endpoint(
    request: Request,
    task_id: str,
    workspace: str | None = Form(None),
    h1_actor: str = Form(...),
    h2_actor: str = Form(...),
    card: UploadFile | None = File(None),
    acceptance_card: UploadFile | None = File(None),
    contract: UploadFile = File(...),
    mapping: UploadFile = File(...),
    repo: str | None = Form(None),
    card_sha256: str = Form(...),
    contract_sha256: str = Form(...),
    mapping_sha256: str = Form(...),
    operation_source: UploadFile | None = File(None),
    push: bool = Form(False),
    asset_manifest: str = Form("[]"),
    assets: list[UploadFile] | None = File(None),
):
    app_ctx = request.app.state.app_ctx
    check_actor_aliases({"h1_actor": h1_actor, "h2_actor": h2_actor})
    repository = operator_repo(app_ctx, repo)
    card_upload = card if card is not None else acceptance_card
    if card_upload is None:
        raise PlatformError("c1.invalid_card", "acceptance card file is required")
    with tempfile.TemporaryDirectory(prefix="aidev-web-freeze-") as temporary:
        card_path = save_upload(card_upload, temporary, "acceptance-card.json")
        contract_path = save_upload(contract, temporary, "contract.json")
        mapping_path = save_upload(mapping, temporary, "mapping.json")
        operation_source_path = save_upload(operation_source, temporary, "operations.json")
        # AC-05：三件套随件摘要校验（不符 → c3.content_hash_mismatch 零副作用）。
        verify_upload_digests(card_path, contract_path, mapping_path, card_sha256, contract_sha256, mapping_sha256)
        materialize_assets(assets, asset_manifest, temporary, contract_path)
        return ok(freeze_task(repository, task_id, workspace, card_path, contract_path, mapping_path, operation_source_path, h1_actor, h2_actor, push))


def refreeze_endpoint(
    request: Request,
    task_id: str,
    workspace: str | None = Form(None),
    h1_actor: str = Form(...),
    h2_actor: str = Form(...),
    reason: str = Form(...),
    card: UploadFile | None = File(None),
    acceptance_card: UploadFile | None = File(None),
    contract: UploadFile = File(...),
    mapping: UploadFile = File(...),
    repo: str | None = Form(None),
    card_sha256: str = Form(...),
    contract_sha256: str = Form(...),
    mapping_sha256: str = Form(...),
    operation_source: UploadFile | None = File(None),
    push: bool = Form(False),
    asset_manifest: str = Form("[]"),
    assets: list[UploadFile] | None = File(None),
):
    app_ctx = request.app.state.app_ctx
    check_actor_aliases({"h1_actor": h1_actor, "h2_actor": h2_actor})
    repository = operator_repo(app_ctx, repo)
    card_upload = card if card is not None else acceptance_card
    if card_upload is None:
        raise PlatformError("c1.invalid_card", "acceptance card file is required")
    with tempfile.TemporaryDirectory(prefix="aidev-web-refreeze-") as temporary:
        card_path = save_upload(card_upload, temporary, "acceptance-card.json")
        contract_path = save_upload(contract, temporary, "contract.json")
        mapping_path = save_upload(mapping, temporary, "mapping.json")
        operation_source_path = save_upload(operation_source, temporary, "operations.json")
        verify_upload_digests(card_path, contract_path, mapping_path, card_sha256, contract_sha256, mapping_sha256)
        materialize_assets(assets, asset_manifest, temporary, contract_path)
        return ok(refreeze_task(repository, task_id, workspace, card_path, contract_path, mapping_path, operation_source_path, h1_actor, h2_actor, reason, push))


async def sync_endpoint(request: Request):
    # AC-08（api-remote-ready）：sync 改内容上传——请求体 {repo?, actor, push,
    # proposals}（proposals 必填，缺失 → 400 request.invalid）；submit 类提案的
    # 证据字节随 multipart 文件上传（按文件名匹配），服务端不按 client 路径读证据；
    # workspace 仅保留为 collab 经典通道（§8 范围外，维持现行服务端读语义），
    # 投影写回整体删除（客户端经任务包拉取自行落地）。
    app_ctx = request.app.state.app_ctx
    content_type = request.headers.get("content-type", "")
    repo = None
    push = False
    proposals = None
    workspace_paths = []
    evidence_lookup = {}
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        repo = form.get("repo")
        push = str(form.get("push") or "").lower() == "true"
        raw_proposals = form.get("proposals")
        if raw_proposals is not None:
            try:
                proposals = json.loads(raw_proposals)
            except (TypeError, ValueError):
                raise PlatformError("request.invalid", "proposals is not valid JSON")
        raw_workspace = form.get("workspace")
        if raw_workspace:
            try:
                workspace_value = json.loads(raw_workspace)
            except (TypeError, ValueError):
                raise PlatformError("request.invalid", "workspace must be a JSON list of workspace paths")
            if not isinstance(workspace_value, list):
                raise PlatformError("request.invalid", "workspace must be a JSON list of workspace paths")
            workspace_paths = [str(item) for item in workspace_value]
        for upload in form.getlist("evidence"):
            if upload is None or not getattr(upload, "filename", None):
                continue
            name = Path(upload.filename).name
            evidence_lookup[name] = upload.file.read()
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            raise PlatformError("request.invalid", "sync body must be a JSON object")
        repo = payload.get("repo")
        push = bool(payload.get("push"))
        proposals = payload.get("proposals")
        workspace_value = payload.get("workspace")
        if workspace_value is not None:
            if not isinstance(workspace_value, list):
                raise PlatformError("request.invalid", "workspace must be a list of workspace paths")
            workspace_paths = [str(item) for item in workspace_value]
    # 异步接线（T16 返场修复，同 verify_endpoint）：synchronize_uploaded 为阻塞
    # 调用（git 锁/提交/远端对账），经线程池执行保持事件循环可响应。校验顺序
    # 保持 repo 解析（AC-01/02）→ proposals 校验 → 应用。
    def run_sync():
        repository = operator_repo(app_ctx, repo)
        if proposals is None:
            raise PlatformError("request.invalid", "proposals is required")
        if not isinstance(proposals, list):
            raise PlatformError("request.invalid", "proposals must be a list")
        return synchronize_uploaded(
            repository,
            proposals,
            evidence_lookup,
            request_actor(request),
            push,
            workspace_paths=workspace_paths,
        )

    result = await run_in_threadpool(run_sync)
    return ok(result)


async def verify_endpoint(request: Request, task_id: str):
    # 手工解析（替代 FastAPI Form 注入）：AC-01/02 要求 repo 路径形态/未知逻辑 id
    # 的拒绝先于字段校验——JSON 体（编排探针）与 multipart 表单（CLI）双形态。
    # T24/REQ-6（AC-03）：multipart 增 evidence 文件通道——按 filename 与 results
    # 引用匹配，逐件 sha256 核验后落 tasks/<id>/verification/ 入账。
    app_ctx = request.app.state.app_ctx
    content_type = request.headers.get("content-type", "")
    temporary = tempfile.TemporaryDirectory(prefix="aidev-web-verify-")
    try:
        results_file = manual_results_file = review_report_file = None
        evidence_uploads = {}
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            repo = form.get("repo")
            verifier = form.get("verifier")
            confirmed_by = form.get("confirmed_by")
            execute = str(form.get("execute") or "").lower() == "true"
            push = str(form.get("push") or "").lower() == "true"
            reverify = str(form.get("reverify") or "").lower() == "true"
            for field, fallback in (
                ("results", "results.json"),
                ("manual_results", "manual-results.json"),
                ("review_report", "ai-review-report.json"),
            ):
                upload = form.get(field)
                if upload is None or not getattr(upload, "filename", None):
                    continue
                path = Path(temporary.name) / fallback
                path.write_bytes(upload.file.read())
                if field == "results":
                    results_file = str(path)
                elif field == "manual_results":
                    manual_results_file = str(path)
                else:
                    review_report_file = str(path)
            for upload in form.getlist("evidence"):
                if upload is None or not getattr(upload, "filename", None):
                    continue
                evidence_uploads[Path(upload.filename).name] = upload.file.read()
        elif content_type.startswith("application/json"):
            payload = await request.json()
            if not isinstance(payload, dict):
                raise PlatformError("request.invalid", "verify body must be a JSON object")
            repo = payload.get("repo")
            verifier = payload.get("verifier")
            confirmed_by = payload.get("confirmed_by")
            execute = str(payload.get("execute") or "").lower() == "true"
            push = str(payload.get("push") or "").lower() == "true"
            reverify = str(payload.get("reverify") or "").lower() == "true"
        else:
            repo = verifier = confirmed_by = None
            execute = push = reverify = False

        # 异步接线（T16 返场修复）：verify_task 为阻塞调用（git 锁/提交/远端对账），
        # 经线程池执行保持事件循环可响应。校验顺序保持 repo 解析 → 字段校验 → verify_task。
        def run_verify():
            repository = operator_repo(app_ctx, repo)
            if not verifier or not confirmed_by:
                raise PlatformError("request.invalid", "verifier and confirmed_by are required")
            check_actor_aliases({"verifier": verifier, "confirmed_by": confirmed_by})
            return verify_task(repository, task_id, verifier, confirmed_by, results_file, execute, manual_results_file, push, review_report_file=review_report_file, reverify=reverify, evidence_uploads=evidence_uploads)

        result = await run_in_threadpool(run_verify)
        return ok(result)
    finally:
        temporary.cleanup()


def reject_endpoint(request: Request, task_id: str, body: RejectBody):
    app_ctx = request.app.state.app_ctx
    repository = operator_repo(app_ctx, body.repo)
    return ok(reject_task(repository, task_id, body.category, body.reason, request_actor(request), body.workspace, body.push))


def close_endpoint(request: Request, task_id: str, body: CloseBody):
    app_ctx = request.app.state.app_ctx
    repository = operator_repo(app_ctx, body.repo)
    return ok(close_task(repository, task_id, body.decision, request_actor(request), body.reason, body.push, ship=body.ship, ship_commit=body.ship_commit, ship_fingerprint=body.ship_fingerprint))


def ship_endpoint(request: Request, task_id: str, body: ShipBody):
    app_ctx = request.app.state.app_ctx
    repository = operator_repo(app_ctx, body.repo)
    return ok(ship_task(repository, task_id, request_actor(request), body.commit_hash, body.workspace_fingerprint, push=body.push))


def void_endpoint(request: Request, task_id: str, body: VoidBody):
    app_ctx = request.app.state.app_ctx
    repository = operator_repo(app_ctx, body.repo)
    return ok(void_task(repository, task_id, body.reason, request_actor(request), body.push))


def adjudication_endpoint(request: Request, task_id: str, body: AdjudicateBody):
    app_ctx = request.app.state.app_ctx
    repository = operator_repo(app_ctx, body.repo)
    return ok(adjudicate_task(repository, task_id, body.category, body.reason, request_actor(request), body.workspace, body.push))


def vetoes_endpoint(
    request: Request,
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
    card_sha256: str | None = Form(None),
    contract_sha256: str | None = Form(None),
    mapping_sha256: str | None = Form(None),
    asset_manifest: str = Form("[]"),
    assets: list[UploadFile] | None = File(None),
):
    app_ctx = request.app.state.app_ctx
    check_actor_aliases({"actor": actor})
    identity = request_actor(request)
    repository = operator_repo(app_ctx, repo)
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
        # AC-04/AC-05：veto-propose 同 freeze 走内容上传 + 随件摘要校验。
        if propose and card_path and contract_path and mapping_path:
            verify_upload_digests(card_path, contract_path, mapping_path, card_sha256, contract_sha256, mapping_sha256)
            materialize_assets(assets, asset_manifest, temporary, contract_path)
        if list_flag:
            return ok({"vetoes": list_vetoes(repository)})
        if propose:
            if not task_id or not card_path or not contract_path or not mapping_path:
                raise PlatformError("veto.missing_arguments", "--task-id --acceptance-card --contract --mapping are required with --propose")
            return ok(propose_veto(repository, task_id, card_path, contract_path, mapping_path, reason, identity, push))
        if not veto_id:
            raise PlatformError("veto.missing_arguments", "--veto-id is required")
        if approve:
            return ok(decide_veto(repository, veto_id, "approve", identity, None, push))
        if reject:
            return ok(decide_veto(repository, veto_id, "reject", identity, reason, push))
        return ok(decide_veto(repository, veto_id, "withdraw", identity, None, push))


def inbox_endpoint(request: Request, repo: str | None = None, task_id: str | None = None):
    app_ctx = request.app.state.app_ctx
    repository = operator_repo(app_ctx, repo)
    return ok(collaboration_overview(repository, task_id))
