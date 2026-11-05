import argparse
import json
import sys

from .errors import PlatformError
from .service import claim, init_repository, init_task, status, submit, synchronize, task_log
from .specs import freeze_task


def parser_for(role):
    parser = argparse.ArgumentParser(prog=f"aidev-{role}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    if role == "operator":
        repository = subparsers.add_parser("init-repository")
        repository.add_argument("--repo", required=True)
        repository.add_argument("--actor", required=True)
        task = subparsers.add_parser("init-task")
        task.add_argument("--repo", required=True)
        task.add_argument("--workspace", required=True)
        task.add_argument("--title", required=True)
        task.add_argument("--requirement", required=True)
        task.add_argument("--owner", required=True)
        task.add_argument("--actor", required=True)
        freeze = subparsers.add_parser("freeze")
        freeze.add_argument("--repo", required=True)
        freeze.add_argument("--task-id", required=True)
        freeze.add_argument("--workspace", required=True)
        freeze.add_argument("--acceptance-card", required=True)
        freeze.add_argument("--contract", required=True)
        freeze.add_argument("--mapping", required=True)
        freeze.add_argument("--operation-source")
        freeze.add_argument("--h1-actor", required=True)
        freeze.add_argument("--h2-actor", required=True)
        freeze.add_argument("--push", action="store_true")
        sync = subparsers.add_parser("sync")
        sync.add_argument("--repo", required=True)
        sync.add_argument("--workspace", action="append", required=True)
        sync.add_argument("--actor", required=True)
        sync.add_argument("--push", action="store_true")
    else:
        claim_parser = subparsers.add_parser("claim")
        claim_parser.add_argument("--workspace", required=True)
        claim_parser.add_argument("--actor", required=True)
        claim_parser.add_argument("--on-behalf-of", required=True)
        submit_parser = subparsers.add_parser("submit")
        submit_parser.add_argument("--workspace", required=True)
        submit_parser.add_argument("--actor", required=True)
        submit_parser.add_argument("--on-behalf-of", required=True)
        submit_parser.add_argument("--report", required=True)
        submit_parser.add_argument("--evidence", action="append", required=True)
        status_parser = subparsers.add_parser("status")
        status_parser.add_argument("--workspace", required=True)
    log_parser = subparsers.add_parser("log")
    log_parser.add_argument("--repo", required=True)
    log_parser.add_argument("--task-id", required=True)
    return parser


def dispatch(role, arguments):
    if arguments.command == "init-repository":
        return init_repository(arguments.repo, arguments.actor)
    if arguments.command == "init-task":
        return init_task(
            arguments.repo,
            arguments.workspace,
            arguments.title,
            arguments.requirement,
            arguments.owner,
            arguments.actor,
        )
    if arguments.command == "freeze":
        return freeze_task(
            arguments.repo,
            arguments.task_id,
            arguments.workspace,
            arguments.acceptance_card,
            arguments.contract,
            arguments.mapping,
            arguments.operation_source,
            arguments.h1_actor,
            arguments.h2_actor,
            arguments.push,
        )
    if arguments.command == "claim":
        return claim(arguments.workspace, arguments.actor, arguments.on_behalf_of)
    if arguments.command == "submit":
        return submit(arguments.workspace, arguments.actor, arguments.on_behalf_of, arguments.report, arguments.evidence)
    if arguments.command == "sync":
        return synchronize(arguments.repo, arguments.workspace, arguments.actor, arguments.push)
    if arguments.command == "status":
        return status(arguments.workspace)
    if arguments.command == "log":
        return {"events": task_log(arguments.repo, arguments.task_id)}
    raise PlatformError("operation.unsupported", f"unsupported command: {arguments.command}")


def main(role, argv=None):
    parser = parser_for(role)
    arguments = parser.parse_args(argv)
    try:
        result = dispatch(role, arguments)
        response = {"ok": True, "result": result}
        exit_code = 0
    except PlatformError as error:
        response = {
            "ok": False,
            "error": {"code": error.code, "message": error.message, "details": error.details},
        }
        exit_code = error.exit_code
    except Exception as error:
        response = {
            "ok": False,
            "error": {"code": "internal.error", "message": str(error), "details": {"type": type(error).__name__}},
        }
        exit_code = 4
    print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    return exit_code
