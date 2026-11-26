from pydantic import BaseModel, model_validator

from ..domain import validate_actor_alias

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
    workspace: str | None = None
    repo: str | None = None
    actor: str | None = None
    on_behalf_of: str | None = None


class ProjectRegisterBody(ActorValidatingModel):
    name: str | None = None
    state_repo: str | None = None
    code_repo: str | None = None
    credentials: dict | None = None
    actor: str | None = None


class ProjectConfigureBody(ActorValidatingModel):
    name: str | None = None
    credentials: dict | None = None
    state_repo: str | None = None
    code_repo: str | None = None
    actor: str | None = None


class CredentialRemoveBody(ActorValidatingModel):
    repo: str | None = None
    actor: str | None = None


class PatRevokeBody(ActorValidatingModel):
    """POST /api/pats/{pat_id}/revoke 的可选 JSON 体（空体合法）。"""

    actor: str | None = None


class LoginBody(BaseModel):
    user: str
    password: str


class UserCreateBody(BaseModel):
    """T23/REQ-5（EP-02）：远程建号请求体。"""

    user: str
    password: str


class UserPasswordBody(BaseModel):
    """T23/REQ-5（EP-04）：远程重置口令请求体。"""

    password: str


class SigtReissueBody(BaseModel):
    """T23/REQ-5（EP-05）：SIGT 重发请求体（repo 省略落任务所属项目）。"""

    actor: str
    repo: str | None = None


class DocPutBody(BaseModel):
    """T23/REQ-5（EP-07）：白名单文档带版本更新请求体（base_sha256 缺省=仅创建）。"""

    base_sha256: str | None = None
    content: str


class EnhancementBody(ActorValidatingModel):
    workspace: str
    actor: str | None = None
    on_behalf_of: str | None = None
    title: str
    details: str


class CollabSendBody(ActorValidatingModel):
    workspace: str
    actor: str | None = None
    on_behalf_of: str | None = None
    summary: str
    type: str | None = None
    message_type: str | None = None
    to_task: str | None = None
    target_task: str | None = None
    details: str | None = None
    body: str | None = None


class CollabAnswerBody(ActorValidatingModel):
    workspace: str
    actor: str | None = None
    on_behalf_of: str | None = None
    action: str
    note: str | None = None


class RepositoryBody(ActorValidatingModel):
    repo: str | None = None
    actor: str | None = None
    name: str | None = None
    owner: str | None = None


class InitTaskBody(ActorValidatingModel):
    repo: str | None = None
    workspace: str | None = None
    title: str
    requirement: str
    owner: str | None = None
    actor: str | None = None
    req_items: list | None = None


class RejectBody(ActorValidatingModel):
    category: str
    reason: str
    actor: str | None = None
    repo: str | None = None
    workspace: str | None = None
    push: bool = False


class CloseBody(ActorValidatingModel):
    decision: str
    actor: str | None = None
    repo: str | None = None
    reason: str | None = None
    push: bool = False
    ship: bool = False
    ship_commit: str | None = None
    ship_fingerprint: str | None = None


class ShipBody(ActorValidatingModel):
    repo: str | None = None
    commit_hash: str
    workspace_fingerprint: str | None = None
    push: bool = False


class VoidBody(ActorValidatingModel):
    reason: str
    actor: str | None = None
    repo: str | None = None
    push: bool = False


class AdjudicateBody(ActorValidatingModel):
    category: str
    reason: str
    actor: str | None = None
    repo: str | None = None
    workspace: str | None = None
    push: bool = False


class PatIssueBody(BaseModel):
    label: str
    scope: list
    expires_in: str
    project_id: str | None = None


class RequirementCreateBody(ActorValidatingModel):
    requirement_id: str
    title: str
    body: str | None = None
    entries: list = []
    source: str | None = None
    repo: str | None = None


class RequirementUpdateBody(ActorValidatingModel):
    body: str | None = None
    entries: list = []
    source: str | None = None
    repo: str | None = None


class MergeBody(BaseModel):
    backlog_task_id: str
    entry_id: str


class ConfirmBody(BaseModel):
    entries: list = []
    repo: str | None = None
