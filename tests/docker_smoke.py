"""Explicit Docker verification; never run implicitly by unittest discovery.

Usage: .venv/bin/python -B tests/docker_smoke.py --image sidim-bez-dim:harness-v8
Only trusted test fixtures are executed. No provider credentials are forwarded.
"""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import shutil
import re
import subprocess
import tempfile


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
for name in (".venv", ".git", "codex-local", "AGENTS.md", ".env", "tests", "__pycache__"):
    assert not Path("/app", name).exists(), name
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
    assert quota == 8 * period
    limits["cpus"] = 8
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


def copies(destination):
    shutil.copytree(EXAMPLES, destination)
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
    parser.add_argument("--image", default="sidim-bez-dim:harness-v8")
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()

    def docker(arguments, expected=0, timeout=300):
        result = subprocess.run([args.docker, *arguments], capture_output=True, text=True, timeout=timeout)
        if result.returncode != expected:
            raise AssertionError(f"Docker returned {result.returncode}, expected {expected}:\n{result.stdout}\n{result.stderr}")
        return result

    def container(arguments, mounts=(), expected=0):
        command = ["run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=2g",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
                   "--memory", "16g", "--cpus", "8", "--workdir", "/app", "--entrypoint", "python",
                   "-e", "HOME=/tmp", "-e", "BUDGET=0"]
        for source, target, readonly in mounts:
            command += ["--mount", f"type=bind,source={source},target={target}" + (",readonly" if readonly else "")]
        return docker([*command, args.image, *arguments], expected=expected)

    material_before = hashes(ROOT / "codex-local")
    image = json.loads(docker(["image", "inspect", args.image]).stdout)[0]
    # With Docker's containerd store inspect.Size and `images .Size` can
    # describe different storage quantities. The rules name the latter.
    size = docker(["image", "ls", "--filter", f"reference={args.image}", "--format", "{{.Size}}"]).stdout.strip()
    match = re.fullmatch(r"([0-9.]+)(B|kB|MB|GB|TB)", size)
    assert match is not None, size
    reported_bytes = Decimal(match[1]) * {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}[match[2]]
    assert reported_bytes < 4_000_000_000, size
    assert not any(value.startswith(("LLM_API_KEY=", "LLM_BASE_URL=")) for value in image["Config"]["Env"])
    audit = json.loads(container(["-c", AUDIT]).stdout)
    dependencies = container(["-m", "pip", "check"]).stdout.strip()

    with tempfile.TemporaryDirectory(prefix="harness-docker-") as temporary:
        base = Path(temporary)
        base.chmod(0o755)
        safe, trusted = base / "safe", base / "trusted"
        copies(safe)
        copies(trusted)
        trusted_before = hashes(trusted)
        standard = container(["/app/run_agent.py", "--tasks-dir", "/data/tasks"], [(safe, "/data/tasks", False)])
        for name, answer in ANSWERS.items():
            path = safe / name / "answer.txt"
            if name.startswith("coding"):
                assert not path.exists()
            else:
                assert path.read_text().strip() == answer
        assert "solved=6 skipped=2 failed=0" in standard.stdout

        # Explicit opt-in for supplied organizer examples only, never inferred
        # from environment or an untrusted task.json.
        trusted_run = container(["/app/run_agent.py", "--tasks-dir", "/data/tasks", "--trusted-code"],
                                [(trusted, "/data/tasks", False)])
        for name, answer in ANSWERS.items():
            assert (trusted / name / "answer.txt").read_text().strip() == answer
        trusted_after = hashes(trusted)
        changed_sources = []
        for name in ("coding_0003", "coding_0007"):
            source, test = f"{name}/workspace/mylib.py", f"{name}/workspace/test_mylib.py"
            assert trusted_before[source] != trusted_after[source]
            assert trusted_before[test] == trusted_after[test]
            changed_sources.append(source)
        assert "solved=8 skipped=0 failed=0" in trusted_run.stdout

        bad = safe / "bad_task"
        bad.mkdir(mode=0o777)
        (bad / "task.json").write_text("not JSON")
        malformed = container(["/app/run_agent.py", "--tasks-dir", "/data/tasks"],
                              [(safe, "/data/tasks", False)], expected=1)
        assert "solved=6 skipped=2 failed=1" in malformed.stdout
        container(["/app/run_agent.py", "--tasks-dir", "/does-not-exist"], expected=2)

        # Verification inputs are read-only mounts, never image/build contents.
        suite = container(["-B", "-m", "unittest", "discover", "-s", "/app/tests", "-v"],
                          [(ROOT / "tests", "/app/tests", True),
                           (ROOT / "codex-local", "/app/codex-local", True)])
        print(suite.stderr, end="")

    assert material_before == hashes(ROOT / "codex-local")
    print(json.dumps({"image": args.image, "docker_images_size": size, "inspect_size_bytes": image["Size"], "audit": audit,
                      "dependencies": dependencies, "safe_examples": "6/8", "trusted_examples": "8/8",
                      "persisted_repairs": changed_sources, "malformed_task_exit": 1,
                      "missing_directory_exit": 2, "source_materials_unchanged": True}, indent=2))


if __name__ == "__main__":
    main()
