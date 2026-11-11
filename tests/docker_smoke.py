"""Explicit Docker verification; never run implicitly by unittest discovery.

Usage: .venv/bin/python -B tests/docker_smoke.py --image sidim-bez-dim:v4-h2 --output /tmp/new-v4-check
Only allowlisted development and reviewed synthetic test fixtures are used.
No closed corpus, private state or provider credentials enter the containers.
"""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import shutil
import re
import stat
import subprocess
import tempfile
import uuid

from allowed_acceptance import prepare_view


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "codex-local" / "examples" / "task-dir"
ANSWERS = {"math_0000": "389", "math_0004": "1735", "search_0001": "1983", "search_0005": "Osaka",
           "data_analysis_0002": "158", "data_analysis_0006": "284", "coding_0003": "DONE", "coding_0007": "DONE"}

# Known code, not code supplied by a task. Executed with all contest restrictions.
AUDIT = r'''
import ctypes, errno, importlib.metadata, json, os
from pathlib import Path
assert importlib.metadata.version("openjiuwen") == "0.1.20.post1"
assert os.environ["HOME"] == "/tmp"
assert not Path("/var/run/docker.sock").exists()
for name in (".venv", ".git", "codex-local", "AGENTS.md", ".env", "tests", "benchmark", "__pycache__"):
    assert not Path("/app", name).exists(), name
assert {path.name for path in Path("/app/diagnostics").iterdir()} == {
    "coding_acceptance.py", "coding_research.py", "static_coding.py",
}
assert not any(key in os.environ for key in ("LLM_API_KEY", "LLM_BASE_URL"))
try:
    Path("/app/forbidden-write").write_text("must fail")
except OSError as exc:
    assert exc.errno == errno.EROFS, exc
else:
    raise AssertionError("/app is writable")
Path("/tmp/write-check").write_text("ok")
mounts = Path("/proc/mounts").read_text().splitlines()
assert any(line.split()[1:3] == ["/tmp", "tmpfs"] for line in mounts)
stat = os.statvfs("/tmp")
assert stat.f_blocks * stat.f_frsize == 2 * 1024**3
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
assert int(status["CapEff"].strip(), 16) == 0
assert status["NoNewPrivs"].strip() == "1"
limits = {}
for name, value in (("memory.max", 16 * 1024**3), ("pids.max", 512)):
    path = Path("/sys/fs/cgroup", name)
    if path.exists():
        limits[name] = int(path.read_text().strip())
        assert limits[name] == value
cpu = Path("/sys/fs/cgroup/cpu.max")
if cpu.exists():
    quota, period = map(int, cpu.read_text().split())
    assert quota == int(os.environ["ACCEPTANCE_CPUS"]) * period
    limits["cpus"] = int(os.environ["ACCEPTANCE_CPUS"])
libc = ctypes.CDLL(None, use_errno=True)
result = libc.unshare(0x10000000)  # Harmless probe for a new user namespace.
namespace_errno = ctypes.get_errno() if result else 0
print(json.dumps({"python": os.sys.version.split()[0], "openjiuwen": importlib.metadata.version("openjiuwen"),
                  "app_read_only": True, "tmpfs_bytes": stat.f_blocks * stat.f_frsize,
                  "cap_eff": status["CapEff"].strip(), "no_new_privileges": True,
                  "cgroup_limits": limits,
                  "user_namespace_errno": namespace_errno, "no_credentials_in_image_environment": True}))
'''


def hashes(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def copies(destination, source=EXAMPLES):
    shutil.copytree(source, destination)
    for task in destination.glob("*/task.json"):
        data = json.loads(task.read_text())
        data.pop("expected", None)
        data.pop("check", None)
        task.write_text(json.dumps(data))
    # Permissions of temporary fixtures only: UID 0 with cap-drop ALL cannot
    # override a host user's 0755 directory when publishing an answer.
    for path in [destination, *destination.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='sidim-bez-dim:v4-h2')
    parser.add_argument('--docker', default='docker')
    parser.add_argument('--output', type=Path, required=True,
                        help='new directory for logs/results; historical runs are never overwritten')
    args = parser.parse_args()
    args.output.mkdir()
    args.output=args.output.resolve()
    args.output.chmod(0o777)
    sequence=0

    def docker(arguments, expected=0, timeout=300):
        nonlocal sequence
        sequence+=1
        result=subprocess.run([args.docker,*arguments],capture_output=True,text=True,timeout=timeout)
        (args.output/f'{sequence:02d}.stdout.log').write_text(result.stdout)
        (args.output/f'{sequence:02d}.stderr.log').write_text(result.stderr)
        if result.returncode!=expected:
            raise AssertionError(f'Docker returned {result.returncode}, expected {expected}: {result.stderr[-2500:]}')
        return result

    def container(arguments=None, mounts=(), expected=0, timeout=300, *, cpus=8, budget=True):
        name='sidim-v4-h2-'+uuid.uuid4().hex[:12]
        command=['run','--rm','--name',name,'--network','none','--read-only','--tmpfs','/tmp:rw,size=2g',
                 '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','512',
                 '--memory','16g','--cpus',str(cpus),'--workdir','/app',
                 '-e','HOME=/tmp','-e',f'ACCEPTANCE_CPUS={cpus}']
        if budget:command+=['-e','BUDGET=0']
        if arguments is not None:command+=['--entrypoint','python']
        for source,target,readonly in mounts:
            command+=['--mount',f'type=bind,source={source},target={target}'+(',readonly' if readonly else '')]
        try:
            return docker([*command,args.image,*(arguments or [])],expected=expected,timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run([args.docker,'rm','-f',name],capture_output=True,timeout=30)
            raise

    material_before=hashes(EXAMPLES)
    image=json.loads(docker(['image','inspect',args.image]).stdout)[0]
    size=docker(['image','ls','--filter',f'reference={args.image}','--format','{{.Size}}']).stdout.strip()
    match=re.fullmatch(r'([0-9.]+)(B|kB|MB|GB|TB)',size)
    assert match,size
    units={'B':1,'kB':1000,'MB':1000**2,'GB':1000**3,'TB':1000**4}
    assert Decimal(match[1])*units[match[2]]<4_000_000_000,size
    assert not any(v.startswith(('LLM_API_KEY=','LLM_BASE_URL=','BUDGET=')) for v in image['Config']['Env'])
    assert image['Config']['Entrypoint']==['python']
    assert image['Config']['Cmd']==['/app/run_agent.py','--tasks-dir','/data/tasks']
    audit=json.loads(container(['-B','-c',AUDIT]).stdout)
    audit4=json.loads(container(['-B','-c',AUDIT],cpus=4).stdout)
    dependencies=container(['-m','pip','check']).stdout.strip()
    inputs=[line[1:] for line in (ROOT/'.dockerignore').read_text().splitlines() if line.startswith('!')]
    expected_hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in inputs}
    actual_hashes=json.loads(container(['-B','-c',
        "import hashlib,json;from pathlib import Path;names="+repr(inputs)+
        ";print(json.dumps({n:hashlib.sha256(Path('/app',n).read_bytes()).hexdigest() for n in names}))"]).stdout)
    assert actual_hashes==expected_hashes,'image differs from current runtime'
    isolation=json.loads(container(['-I','-S','-B','/audit/probe.py'],
        [(ROOT/'tests/coding_isolation_probe.py','/audit/probe.py',True)]).stdout)
    assert isolation['sandbox_confirmed'] is False
    assert isolation['task_code_executed'] is False
    for name,experiment in isolation.get('experiments',{}).items():
        assert not experiment['timed_out']
        assert experiment['returncode']==(-9 if name=='cpu' else 0)

    with tempfile.TemporaryDirectory(prefix='harness-allowed-docker-') as temporary:
        base=Path(temporary);base.chmod(0o755)
        view=base/'view';view_info=prepare_view(view)
        safe=base/'safe';copies(safe,view/'codex-local/examples/task-dir')
        before=hashes(safe)
        # Actual image ENTRYPOINT/CMD, no extra arguments and no BUDGET variable.
        standard=container(mounts=[(safe,'/data/tasks',False)],budget=False)
        assert 'solved=6 skipped=2 failed=0' in standard.stdout
        assert 'budget=not_set' in standard.stdout
        for key,answer in ANSWERS.items():
            path=safe/key/'answer.txt'
            if key.startswith('coding'):assert not path.exists()
            else:
                assert path.read_text().strip()==answer
                assert stat.S_IMODE(path.stat().st_mode)==0o644
        assert all(hashlib.sha256((safe/name).read_bytes()).hexdigest()==sha for name,sha in before.items())
        bad=safe/'bad_task';bad.mkdir(mode=0o777);(bad/'task.json').write_text('not JSON')
        malformed=container(['/app/run_agent.py','--tasks-dir','/data/tasks'],[(safe,'/data/tasks',False)],expected=1)
        assert 'solved=6 skipped=2 failed=1' in malformed.stdout
        container(['/app/run_agent.py','--tasks-dir','/does-not-exist'],expected=2)
        # Each public directory/file is a separate read-only test mount. The
        # actual image runtime is never replaced by a host repository overlay.
        mounts=[(child,'/app/'+child.name,True) for child in sorted(view.iterdir())]
        mounts.append((args.output,'/audit-result',False))
        container(['-B','/app/tests/allowed_acceptance.py','suite','--output','/audit-result/suite.json'],
                  mounts,timeout=900)
        suite=json.loads((args.output/'suite.json').read_text())
        assert (suite['discovered'],suite['passed'],len(suite['skipped']))==(894,887,7),suite
        assert not suite['failures'] and not suite['errors']
        container(['-B','/app/tests/allowed_acceptance.py','development','--output','/audit-result/development.json'],
                  mounts,timeout=600)
        development=json.loads((args.output/'development.json').read_text())
    assert material_before==hashes(EXAMPLES),'official source examples changed'
    report=dict(image=args.image,image_id=image['Id'],docker_images_size=size,inspect_size_bytes=image['Size'],
                matching_runtime_files=len(inputs),runtime_sha256=actual_hashes,audit=audit,audit_4_cpu=audit4,
                dependencies=dependencies,isolation_research=isolation,view=view_info,suite=suite,
                development={k:{n:v[n] for n in ('correct','wrong','skipped','reward','mock_requests','qualification')}
                             for k,v in development['arms'].items()},
                official_examples_safe='6/8',answer_mode='0644',default_entrypoint=True,missing_budget_safe=True,
                malformed_task_exit=1,missing_directory_exit=2,trusted_task_execution=False,
                closed_corpus_mounted=False,real_api_requests=0,source_materials_unchanged=True)
    with (args.output/'acceptance.json').open('x') as stream:json.dump(report,stream,indent=2);stream.write('\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
