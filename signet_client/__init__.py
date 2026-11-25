"""signet_client 公开兼容面——T21 模块化后纯重导出（零自有实现）。

公开名（含 ClientInputError/ServiceUnavailable 与公开常量）全部自子模块重导出；
入口脚本 `from signet_client import run` 与 T18 冻结案
`from signet_client import local_git_ship` 钉名保持。仅标准库，零 aidev 依赖。

T22/REQ-2（AC-05/AC-07）：客户端不再持有服务端签名密钥、不做本地 HMAC 验签——
凭据域公开面以 parse_sigt（纯格式解析）取代旧本地验签/密钥读取三件套；
handoff-inspect 输出 verified=false 并标注「未验签」，真实性一律服务端裁决。
T22/REQ-1（AC-03）：API_VERSION 钉名常量自 http 域重导出（包内唯一模块级定义）。
"""

from .http import (
    API_VERSION,
    CLIENT_FILE,
    ClientInputError,
    DEFAULT_BASE_URL,
    HTTP_EXIT_CODES,
    REQUEST_TIMEOUT,
    ROLE_HEADER,
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
    parse_sigt,
    resolve_cli_user,
    resolve_token,
    store_client_config,
    workspace_handoff_token,
)
from .projection import (
    WORKSPACE_DIRECTORIES,
    contract_assets,
    handoff_claim,
    land_handoff_workspace,
    land_projection,
    pull_package,
    read_upload,
    resolve_task_id,
)
from .legacy_ship import (
    local_git_ship,
    run_local_ship,
)
from .requests import (
    build_request,
    present,
)
from .cli import (
    dispatch_request,
    land_after_success,
    parser_for,
    run,
)
