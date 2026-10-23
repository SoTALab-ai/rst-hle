"""Run on an existing personal VM, forwarding API env over encrypted SSH stdin (never argv or disk)."""
import argparse
import json
import os
import shlex
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True, help="Existing SSH host alias")
parser.add_argument("--directory", default="~/Developer/rst-hle")
parser.add_argument("--runtime-env", default="work/private/runtime.env")
parser.add_argument("command", nargs=argparse.REMAINDER, help="Arguments after 'uv run', e.g. diamond doctor")
args = parser.parse_args()
command = args.command
if command and command[0] == "--":
    command = command[1:]
if not command:
    parser.error("A command is required")
forwarded = {key: os.environ[key] for key in ("NEW_API_BASE_URL", "NEW_API_KEY") if key in os.environ}
remote = ("import os,sys,json,pathlib; p=json.load(sys.stdin); "
          "os.chdir(os.path.expanduser(p['directory'])); "
          "f=pathlib.Path(p['runtime_env']); "
          "r=dict(line.split('=',1) for line in f.read_text().splitlines() if line and not line.startswith('#')) if f.exists() else {}; "
          "os.execvpe('uv',['uv','run']+p['command'],dict(os.environ,**r,**p['env']))")
result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", args.host, "python3 -c " + shlex.quote(remote)],
                        input=json.dumps({"directory": args.directory, "command": command, "env": forwarded,
                                          "runtime_env": args.runtime_env}),
                        text=True, check=False)
raise SystemExit(result.returncode)
