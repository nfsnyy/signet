import argparse
import json
import sys

from .collaboration import collab_answer, collab_send, collaboration_overview
from .errors import PlatformError
from .lifecycle import adjudicate_task, close_task, reject_task, verify_task, void_task
from .service import block, claim, enhancement, init_repository, init_task, status, submit, synchronize, task_log
from .specs import freeze_task, refreeze_task
from .vetoes import decide_veto, list_vetoes, propose_veto, settle_expired_vetoes


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
        refreeze = subparsers.add_parser("refreeze")
        refreeze.add_argument("--repo", required=True)
        refreeze.add_argument("--task-id", required=True)
        refreeze.add_argument("--workspace", required=True)
        refreeze.add_argument("--acceptance-card", required=True)
        refreeze.add_argument("--contract", required=True)
        refreeze.add_argument("--mapping", required=True)
        refreeze.add_argument("--operation-source")
        refreeze.add_argument("--h1-actor", required=True)
        refreeze.add_argument("--h2-actor", required=True)
        refreeze.add_argument("--reason", required=True)
        refreeze.add_argument("--push", action="store_true")
        verify = subparsers.add_parser("verify")
        verify.add_argument("--repo", required=True)
        verify.add_argument("--task-id", required=True)
        verify.add_argument("--results")
        verify.add_argument("--execute", action="store_true")
        verify.add_argument("--manual-results")
        verify.add_argument("--verifier", required=True)
        verify.add_argument("--confirmed-by", required=True)
        verify.add_argument("--push", action="store_true")
        reject = subparsers.add_parser("reject")
        reject.add_argument("--repo", required=True)
        reject.add_argument("--task-id", required=True)
        reject.add_argument("--category", choices=["implementation", "input"], required=True)
        reject.add_argument("--reason", required=True)
        reject.add_argument("--actor", required=True)
        reject.add_argument("--workspace")
        reject.add_argument("--push", action="store_true")
        close = subparsers.add_parser("close")
        close.add_argument("--repo", required=True)
        close.add_argument("--task-id", required=True)
        close.add_argument("--decision", choices=["accept", "reject"], required=True)
        close.add_argument("--actor", required=True)
        close.add_argument("--reason")
        close.add_argument("--push", action="store_true")
        void = subparsers.add_parser("void")
        void.add_argument("--repo", required=True)
        void.add_argument("--task-id", required=True)
        void.add_argument("--reason", choices=["definition-defect", "cancel", "specification-invalidated"], required=True)
        void.add_argument("--actor", required=True)
        void.add_argument("--push", action="store_true")
        adjudicate = subparsers.add_parser("adjudicate")
        adjudicate.add_argument("--repo", required=True)
        adjudicate.add_argument("--task-id", required=True)
        adjudicate.add_argument("--category", choices=["implementation", "definition"], required=True)
        adjudicate.add_argument("--reason", required=True)
        adjudicate.add_argument("--actor", required=True)
        adjudicate.add_argument("--workspace")
        adjudicate.add_argument("--push", action="store_true")
        sync = subparsers.add_parser("sync")
        sync.add_argument("--repo", required=True)
        sync.add_argument("--workspace", action="append", required=True)
        sync.add_argument("--actor", required=True)
        sync.add_argument("--push", action="store_true")
        veto = subparsers.add_parser("veto")
        veto.add_argument("--repo", required=True)
        veto.add_argument("--actor", required=True)
        veto.add_argument("--task-id")
        veto.add_argument("--veto-id")
        veto.add_argument("--acceptance-card")
        veto.add_argument("--contract")
        veto.add_argument("--mapping")
        veto.add_argument("--reason")
        veto.add_argument("--propose", action="store_true")
        veto.add_argument("--approve", action="store_true")
        veto.add_argument("--reject", action="store_true")
        veto.add_argument("--withdraw", action="store_true")
        veto.add_argument("--list", action="store_true")
        veto.add_argument("--push", action="store_true")
        inbox = subparsers.add_parser("inbox")
        inbox.add_argument("--repo", required=True)
        inbox.add_argument("--task-id")
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
        block_parser = subparsers.add_parser("block")
        block_parser.add_argument("--workspace", required=True)
        block_parser.add_argument("--actor", required=True)
        block_parser.add_argument("--on-behalf-of", required=True)
        block_parser.add_argument("--reason", required=True)
        block_parser.add_argument("--evidence", action="append", required=True)
        enhancement_parser = subparsers.add_parser("enhancement")
        enhancement_parser.add_argument("--workspace", required=True)
        enhancement_parser.add_argument("--actor", required=True)
        enhancement_parser.add_argument("--on-behalf-of", required=True)
        enhancement_parser.add_argument("--title", required=True)
        enhancement_parser.add_argument("--details", required=True)
        status_parser = subparsers.add_parser("status")
        status_parser.add_argument("--workspace", required=True)
        collab_send_parser = subparsers.add_parser("collab-send")
        collab_send_parser.add_argument("--workspace", required=True)
        collab_send_parser.add_argument("--actor", required=True)
        collab_send_parser.add_argument("--on-behalf-of", required=True)
        collab_send_parser.add_argument("--type", choices=["in-spec-request", "definition-doubt", "new-scope"], required=True)
        collab_send_parser.add_argument("--to-task", required=True)
        collab_send_parser.add_argument("--summary", required=True)
        collab_send_parser.add_argument("--details", required=True)
        collab_answer_parser = subparsers.add_parser("collab-answer")
        collab_answer_parser.add_argument("--workspace", required=True)
        collab_answer_parser.add_argument("--actor", required=True)
        collab_answer_parser.add_argument("--on-behalf-of", required=True)
        collab_answer_parser.add_argument("--message-id", required=True)
        collab_answer_parser.add_argument("--action", choices=["accept", "reject", "counter", "escalate", "close"], required=True)
        collab_answer_parser.add_argument("--note")
    log_parser = subparsers.add_parser("log")
    log_parser.add_argument("--repo", required=True)
    log_parser.add_argument("--task-id", required=True)
    return parser


def dispatch(role, arguments):
    if arguments.command == "init-repository":
        return init_repository(arguments.repo, arguments.actor)
    if arguments.command == "init-task":
        return init_task(arguments.repo, arguments.workspace, arguments.title, arguments.requirement, arguments.owner, arguments.actor)
    if arguments.command == "freeze":
        return freeze_task(arguments.repo, arguments.task_id, arguments.workspace, arguments.acceptance_card, arguments.contract, arguments.mapping, arguments.operation_source, arguments.h1_actor, arguments.h2_actor, arguments.push)
    if arguments.command == "refreeze":
        return refreeze_task(arguments.repo, arguments.task_id, arguments.workspace, arguments.acceptance_card, arguments.contract, arguments.mapping, arguments.operation_source, arguments.h1_actor, arguments.h2_actor, arguments.reason, arguments.push)
    if arguments.command == "claim":
        return claim(arguments.workspace, arguments.actor, arguments.on_behalf_of)
    if arguments.command == "submit":
        return submit(arguments.workspace, arguments.actor, arguments.on_behalf_of, arguments.report, arguments.evidence)
    if arguments.command == "block":
        return block(arguments.workspace, arguments.actor, arguments.on_behalf_of, arguments.reason, arguments.evidence)
    if arguments.command == "enhancement":
        return enhancement(arguments.workspace, arguments.actor, arguments.on_behalf_of, arguments.title, arguments.details)
    if arguments.command == "sync":
        return synchronize(arguments.repo, arguments.workspace, arguments.actor, arguments.push)
    if arguments.command == "verify":
        return verify_task(arguments.repo, arguments.task_id, arguments.verifier, arguments.confirmed_by, arguments.results, arguments.execute, arguments.manual_results, arguments.push)
    if arguments.command == "reject":
        return reject_task(arguments.repo, arguments.task_id, arguments.category, arguments.reason, arguments.actor, arguments.workspace, arguments.push)
    if arguments.command == "close":
        return close_task(arguments.repo, arguments.task_id, arguments.decision, arguments.actor, arguments.reason, arguments.push)
    if arguments.command == "void":
        return void_task(arguments.repo, arguments.task_id, arguments.reason, arguments.actor, arguments.push)
    if arguments.command == "adjudicate":
        return adjudicate_task(arguments.repo, arguments.task_id, arguments.category, arguments.reason, arguments.actor, arguments.workspace, arguments.push)
    if arguments.command == "veto":
        actions = [name for name in ("propose", "approve", "reject", "withdraw", "list") if getattr(arguments, name)]
        if len(actions) != 1:
            raise PlatformError("veto.invalid_action", "provide exactly one of --propose --approve --reject --withdraw --list")
        if arguments.list:
            return {"vetoes": list_vetoes(arguments.repo)}
        if arguments.propose:
            if not arguments.task_id or not arguments.acceptance_card or not arguments.contract or not arguments.mapping:
                raise PlatformError("veto.missing_arguments", "--task-id --acceptance-card --contract --mapping are required with --propose")
            return propose_veto(arguments.repo, arguments.task_id, arguments.acceptance_card, arguments.contract, arguments.mapping, arguments.reason, arguments.actor, arguments.push)
        if not arguments.veto_id:
            raise PlatformError("veto.missing_arguments", "--veto-id is required")
        if arguments.approve:
            return decide_veto(arguments.repo, arguments.veto_id, "approve", arguments.actor, None, arguments.push)
        if arguments.reject:
            return decide_veto(arguments.repo, arguments.veto_id, "reject", arguments.actor, arguments.reason, arguments.push)
        return decide_veto(arguments.repo, arguments.veto_id, "withdraw", arguments.actor, None, arguments.push)
    if arguments.command == "status":
        return status(arguments.workspace)
    if arguments.command == "collab-send":
        return collab_send(arguments.workspace, arguments.actor, arguments.on_behalf_of, arguments.type, arguments.to_task, arguments.summary, arguments.details)
    if arguments.command == "collab-answer":
        return collab_answer(arguments.workspace, arguments.actor, arguments.on_behalf_of, arguments.message_id, arguments.action, arguments.note)
    if arguments.command == "inbox":
        return collaboration_overview(arguments.repo, arguments.task_id)
    if arguments.command == "log":
        return {"events": task_log(arguments.repo, arguments.task_id)}
    raise PlatformError("operation.unsupported", f"unsupported command: {arguments.command}")


def main(role, argv=None):
    parser = parser_for(role)
    arguments = parser.parse_args(argv)
    try:
        if role == "operator" and getattr(arguments, "repo", None):
            settle_expired_vetoes(arguments.repo)
        result = dispatch(role, arguments)
        response = {"ok": True, "result": result}
        exit_code = 0
    except PlatformError as error:
        response = {"ok": False, "error": {"code": error.code, "message": error.message, "details": error.details}}
        exit_code = error.exit_code
    except Exception as error:
        response = {"ok": False, "error": {"code": "internal.error", "message": str(error), "details": {"type": type(error).__name__}}}
        exit_code = 4
    print(json.dumps(response, ensure_ascii=True, sort_keys=True))
    return exit_code
