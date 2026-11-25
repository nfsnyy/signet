"""signet_client 公开兼容面——T21 模块化后纯重导出（零自有实现）。

34 个钉名公开名（含 ClientInputError/ServiceUnavailable）与公开常量全部自子模块
重导出；入口脚本 `from signet_client import run` 与 T18 冻结案
`from signet_client import local_git_ship` 钉名保持。仅标准库，零 aidev 依赖。
"""

from .http import (
    CLIENT_FILE,
    ClientInputError,
    DEFAULT_BASE_URL,
    HTTP_EXIT_CODES,
    REQUEST_TIMEOUT,
    ROLE_HEADER,
    SECRET_FILE,
    ServiceUnavailable,
    client_config_path,
    decode_json,
    emit_error,
    emit_unreachable,
    encode_multipart,
    perform,
    resolve_base_url,
    timeout_seconds,
    user_home,
)
from .credentials import (
    WORKSPACE_TOKEN_FILE,
    inspect_handoff,
    read_server_secret,
    resolve_cli_user,
    resolve_token,
    server_secret_candidates,
    store_client_config,
    verify_sigt_local,
    workspace_handoff_token,
)
from ._pending import (
    WORKSPACE_DIRECTORIES,
    build_request,
    contract_assets,
    dispatch_request,
    handoff_claim,
    land_after_success,
    land_handoff_workspace,
    land_projection,
    local_git_ship,
    parser_for,
    present,
    pull_package,
    read_upload,
    resolve_task_id,
    run,
    run_local_ship,
)
