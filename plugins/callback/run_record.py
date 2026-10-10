# Apache-2.0
"""Run record callback (CHG-02, CHG-03): who ran which playbook at which commit, on which hosts, with which result."""

from __future__ import annotations

DOCUMENTATION = """
  name: run_record
  type: aggregate
  short_description: Writes a JSON run record (who, commit, playbook, hosts, result) to journald
  description:
    - At the end of every ansible-playbook run, builds one JSON record with the person or CI job that
      started it, the commit, the playbook, the inventory, limit and tags, check/diff mode and the
      per-host task counts (in check mode, changed tasks are drift).
    - Logs the record on the controller with the identifier ansible-run (journald, shipped to the SIEM)
      and writes it to the file named by ANSIBLE_RUN_RECORD_FILE, from which tools/ansible-run logs
      it on every host the run touched (playbooks/run_record.yml).
    - Extra variable values are never recorded, only their names (they may hold secrets).
  options:
    record_file:
      description: Where to write the JSON record.
      env:
        - name: ANSIBLE_RUN_RECORD_FILE
      ini:
        - section: run_record
          key: file
      default: ""
    skip:
      description: Do not record this run (used for the run that logs the record on the hosts).
      env:
        - name: ANSIBLE_RUN_RECORD_SKIP
      type: bool
      default: false
"""

import datetime
import getpass
import json
import os
import shutil
import socket
import subprocess

from ansible import context
from ansible.plugins.callback import CallbackBase

CI_ACTOR_VARS = ("FORGEJO_ACTOR", "GITEA_ACTOR", "GITHUB_ACTOR")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(cwd: str, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10,
                              check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _ci() -> dict:
    env = os.environ
    actor = next((env[v] for v in CI_ACTOR_VARS if env.get(v)), "")
    server = env.get("GITHUB_SERVER_URL", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    return {
        "ci": bool(env.get("CI") or actor),
        "actor": actor,
        "event": env.get("GITHUB_EVENT_NAME", ""),
        "workflow": env.get("GITHUB_WORKFLOW", ""),
        "ref": env.get("GITHUB_REF", ""),
        "run_url": f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else "",
    }


def _log(identifier: str, message: str) -> None:
    """journald on the controller; systemd-cat keeps long lines whole, logger is the fallback."""
    try:
        if shutil.which("systemd-cat"):
            subprocess.run(["systemd-cat", f"--identifier={identifier}", "--priority=notice"], input=message,
                           text=True, timeout=10, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("logger"):
            subprocess.run(["logger", "--size", "65536", "-t", identifier, "--", message], timeout=10, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "run_record"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.playbook = ""
        self.started = _now()

    def v2_playbook_on_start(self, playbook):
        self.playbook = os.path.abspath(playbook._file_name)
        self.started = _now()

    def v2_playbook_on_stats(self, stats):
        try:
            if self.get_option("skip"):
                return
            self._record(stats)
        except Exception as exc:  # noqa: BLE001 -- recording must never break a run
            self._display.warning(f"run_record: could not write the run record: {exc}")

    def _record(self, stats):
        args = context.CLIARGS
        repo_dir = os.path.dirname(self.playbook) or "."
        commit = os.environ.get("GITHUB_SHA") or _git(repo_dir, "rev-parse", "HEAD")
        hosts = {}
        for host in sorted(stats.processed.keys()):
            summary = stats.summarize(host)
            hosts[host] = {k: summary.get(k, 0) for k in ("ok", "changed", "failures", "unreachable", "skipped", "rescued")}
        failed = any(h["failures"] or h["unreachable"] for h in hosts.values())
        extra_vars = args.get("extra_vars") or ()
        record = {
            "schema": 1,
            "who": {"user": getpass.getuser(), "controller": socket.getfqdn(), **_ci()},
            "what": {
                "playbook": os.path.relpath(self.playbook, _git(repo_dir, "rev-parse", "--show-toplevel") or repo_dir),
                "commit": commit,
                "dirty": bool(_git(repo_dir, "status", "--porcelain")),
                "inventory": list(args.get("inventory") or []),
                "limit": args.get("subset") or "",
                "tags": list(args.get("tags") or []),
                "skip_tags": list(args.get("skip_tags") or []),
                "check_mode": bool(args.get("check")),
                "diff": bool(args.get("diff")),
                "extra_vars_given": len(extra_vars),
            },
            "when": {"start": self.started, "end": _now()},
            "result": "failed" if failed else "ok",
            "changed_tasks": sum(h["changed"] for h in hosts.values()),
            "hosts": hosts,
        }
        message = json.dumps(record, sort_keys=True, separators=(",", ":"))
        _log("ansible-run", message)
        path = self.get_option("record_file")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record, indent=1, sort_keys=True) + "\n")
        self._display.display(
            f"run record: {record['what']['playbook']} commit={commit[:12] or '?'} by "
            f"{record['who']['actor'] or record['who']['user']} result={record['result']} "
            f"changed={record['changed_tasks']} hosts={len(hosts)}")
