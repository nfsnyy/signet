"""aidev.projects 公开兼容面——T21 模块化后纯重导出（零自有实现）。

48 个钉名函数与公开常量全部自子模块重导出；subprocess 模块属性（tests
mock.patch("aidev.projects.subprocess.run") 兼容面）、run_git 与 GIT_IDENTITY
钉名保持。_pending 为 S1-S3 过渡承载，S4 起消失。
"""

import subprocess

from ..gitrepo import run_git
from .registry import (
    CLONES_DIR_NAME,
    CREDENTIALS_FILE,
    EMBEDDED_CREDENTIALS_RE,
    GIT_IDENTITY,
    PATH_FORM_DRIVE_RE,
    PATH_FORM_SEPARATOR_RE,
    PROJECT_MARKER,
    PROJECT_RECORD_KEYS,
    REGISTRY_FILE,
    SCP_LIKE_RE,
    URL_SCHEME_RE,
    builtin_record,
    clone_dir,
    clones_root,
    credentials_path,
    data_dir,
    ensure_builtin,
    get_project,
    is_path_form_repo,
    is_platform_derived,
    is_url_form,
    list_projects,
    load_credentials,
    load_registry,
    normalize_path_value,
    project_id_for_repository,
    put_project,
    read_project_marker,
    registry_path,
    save_credentials,
    save_registry,
    search_task_repo,
    update_project,
    url_scheme,
    validate_repo_url,
    write_project_marker,
)
from .credentials import (
    ASKPASS_DIR_NAME,
    ASKPASS_TEMPLATE,
    GIT_METHODS,
    SSH_KEY_MARKERS,
    _credstate_path,
    apply_credential_to_clone,
    askpass_dir,
    askpass_path,
    credential_required_for,
    get_git_credential,
    git_auth_env,
    git_credential_metadata,
    looks_like_ssh_private_key,
    refresh_askpass_scripts,
    remote_auth_args,
    remove_askpass_script,
    remove_git_credential,
    upsert_git_credential,
    validate_credential,
    write_askpass_script,
)
from .clones import (
    ensure_clone,
    fetch_integrate,
)
from ._pending import (
    GETTING_STARTED_BODY,
    HINTS_TEMPLATE,
    SKELETON_DIRS,
    SKELETON_FILES,
    _probe_clone,
    project_credentials_view,
    register_project,
    skeleton_tree,
    validate_supplied_credentials,
)
