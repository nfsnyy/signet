"""signet_client 公开兼容面——T21 模块化后纯重导出（零自有实现）。

公开名（含 ClientInputError/ServiceUnavailable 与公开常量）全部自子模块重导出；
入口脚本 `from signet_client import run` 与 T18 冻结案
`from signet_client import local_git_ship` 钉名保持。仅标准库，零 aidev 依赖。

T22/REQ-2（AC-05/AC-07）：客户端不再持有服务端签名密钥、不做本地 HMAC 验签——
凭据域公开面以 parse_sigt（纯格式解析）取代旧本地验签/密钥读取三件套；
handoff-inspect 输出 verified=false 并标注「未验签」，真实性一律服务端裁决。
T22/REQ-1（AC-03）：API_VERSION 钉名常量自 http 域重导出（包内唯一模块级定义）。
T24/REQ-7（AC-09）：CLI ship 动词与 close --ship 本机路径退役（argparse 不识、
零本机 git 副作用）；legacy_ship 模块保留在仓内供 T18/T23 冻结案函数级消费，
但不再进入客户端 zip（distribution 钉名成员清单已移除）——本命名空间对
legacy_ship 的 re-export 因此容忍其缺席：仓内照常提供 local_git_ship/
run_local_ship，zip 内包本体可导入、访问该二名按 ImportError 失败。
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
try:
    from .legacy_ship import (
        local_git_ship,
        run_local_ship,
    )
except ImportError:
    # 版本化 zip 薄客户端包钉名成员清单不含 legacy_ship.py（T24/REQ-7 退役出包）：
    # 包本体必须可导入可用，本机 ship 命名空间仅仓内提供；zip 内访问该名按
    # ImportError 失败（ship 动词已退役，CLI 面无引用）。
    pass
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
