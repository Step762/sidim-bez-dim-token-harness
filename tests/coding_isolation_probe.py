"""Explicit, local-only kernel experiments. NOT a sandbox or a task runner.

Run inside the contest container, with no credentials. Each experiment uses
a fresh interpreter and synthetic files. No task source is read or executed.
Syscall numbers/structures are from Linux x86_64 UAPI headers. Other platforms
are reported as unsupported, never treated as evidence of isolation.
"""

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import resource
import signal
import socket
import subprocess
import sys
import tempfile


NAMESPACES = {"user": 0x10000000, "mount": 0x00020000, "pid": 0x20000000,
              "network": 0x40000000, "ipc": 0x08000000, "uts": 0x04000000,
              "cgroup": 0x02000000}
LIBC = ctypes.CDLL(None, use_errno=True)


def call(function, *arguments):
    ctypes.set_errno(0)
    result = function(*arguments)
    return {"result": result, "errno": ctypes.get_errno() if result < 0 else 0}


def attempt(operation):
    try:
        operation()
        return {"allowed": True, "errno": 0}
    except OSError as exc:
        return {"allowed": False, "errno": exc.errno}


def no_new_privileges():
    result = call(LIBC.prctl, 38, 1, 0, 0, 0)
    if result["result"] != 0:
        raise OSError(result["errno"], "cannot enable no_new_privileges")


def landlock(allowed):
    # ABI 3 is deliberately tested even if a newer ABI exists on the host.
    version = call(LIBC.syscall, 444, 0, 0, 1)
    if version["result"] < 3:
        return {"abi": version, "installed": False}

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class Beneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    rights = (1 << 15) - 1  # All filesystem rights through ABI 3, incl. truncate.
    attr = Ruleset(rights)
    rules = call(LIBC.syscall, 444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if rules["result"] < 0:
        return {"abi": version, "installed": False, "create": rules}
    fd = rules["result"]
    parent = os.open(allowed, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        path = Beneath(rights, parent)
        added = call(LIBC.syscall, 445, fd, 1, ctypes.byref(path), 0)
        if added["result"] != 0:
            return {"abi": version, "installed": False, "add": added}
        no_new_privileges()
        restricted = call(LIBC.syscall, 446, fd, 0)
        return {"abi": version, "installed": restricted["result"] == 0,
                "restrict": restricted}
    finally:
        os.close(parent)
        os.close(fd)


def seccomp():
    """A diagnostic DENYLIST, deliberately not a production security policy."""
    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    # Kill on any ABI other than native x86_64. Deny the x32 syscall range too.
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC000003E),
                    (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0),
                    (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    for number in (41, 53, 56, 57, 58, 59, 62, 101, 112, 234, 272, 308, 310, 311, 322, 435):
        instructions += [(0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    instructions.append((0x06, 0, 0, 0x7FFF0000))
    filters = (Filter * len(instructions))(*(Filter(*item) for item in instructions))
    program = Program(len(filters), filters)
    no_new_privileges()
    result = call(LIBC.prctl, 22, 2, ctypes.byref(program), 0, 0)
    return {"installed": result["result"] == 0, "prctl": result,
            "policy": "diagnostic denylist; NOT a sandbox"}


def open_socket():
    # Creating a socket makes no connection and sends no traffic.
    with socket.socket():
        pass


def filesystem_checks(root):
    return {
        "read_own": attempt(lambda: (root / "allowed/own").read_bytes()),
        "write_own": attempt(lambda: (root / "allowed/output").write_bytes(b"probe")),
        "read_outside": attempt(lambda: (root / "outside/canary").read_bytes()),
        "write_outside": attempt(lambda: (root / "outside/output").write_bytes(b"probe")),
        "list_outside": attempt(lambda: os.listdir(root / "outside")),
        "read_via_symlink": attempt(lambda: (root / "allowed/link").read_bytes()),
        "stat_outside": attempt(lambda: (root / "outside/canary").stat()),
        "readlink_outside": attempt(lambda: os.readlink(root / "outside/link")),
    }


def child(case, root):
    if case in NAMESPACES:
        return call(LIBC.unshare, NAMESPACES[case])
    if case == "uid":
        return {"before": [os.getuid(), os.getgid()],
                "setgroups": attempt(lambda: os.setgroups([])),
                "setgid": attempt(lambda: os.setgid(65534)),
                "setuid": attempt(lambda: os.setuid(65534)),
                "after": [os.getuid(), os.getgid()]}
    if case == "chroot":
        return attempt(lambda: os.chroot(root / "allowed"))
    if case in {"landlock", "seccomp", "combined"}:
        result = {}
        # Deliberately keep ONE synthetic descriptor to measure this limitation.
        # No actual service descriptor or credential is inherited by the child.
        before = os.open(root / "outside/canary", os.O_RDONLY | os.O_CLOEXEC)
        if case in {"landlock", "combined"}:
            result["landlock"] = landlock(root / "allowed")
        if case in {"seccomp", "combined"}:
            result["seccomp"] = seccomp()
        result["files"] = filesystem_checks(root)
        result["preopened_outside_fd"] = attempt(lambda: os.read(before, 1))
        os.close(before)
        result["socket"] = attempt(open_socket)
        # Signal 0 only checks permission; never sends a signal to the parent.
        result["parent_signal_zero"] = attempt(lambda: os.kill(os.getppid(), 0))
        if case in {"seccomp", "combined"} and result["seccomp"]["installed"]:
            # Invalid clone arguments cannot create a child even without a filter.
            result["invalid_clone"] = call(LIBC.syscall, 56, ctypes.c_ulong(-1), 0, 0, 0, 0)
            result["invalid_clone3"] = call(LIBC.syscall, 435, 0, 0)
            result["setsid"] = call(LIBC.setsid)
        return result
    if case == "nproc":
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        try:
            pid = os.fork()  # One known child, exits immediately; never a fork bomb.
        except OSError as exc:
            return {"fork_allowed": False, "errno": exc.errno, "uid": os.getuid()}
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        return {"fork_allowed": True, "errno": 0, "uid": os.getuid()}
    if case == "rlimits":
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        # At most 32 MiB is requested; no memory-pressure experiment on the host.
        virtual = int(Path("/proc/self/statm").read_text().split()[0]) * os.sysconf("SC_PAGE_SIZE")
        resource.setrlimit(resource.RLIMIT_AS, (virtual + 16 * 1024**2,) * 2)
        try:
            bytearray(32 * 1024**2)
            memory_denied = False
        except MemoryError:
            memory_denied = True
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))
        file_result = attempt(lambda: (root / "allowed/large").write_bytes(b"x" * 2048))
        return {"memory_allocation_denied": memory_denied, "large_file": file_result,
                "file_size": (root / "allowed/large").stat().st_size,
                "nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
                "nproc": resource.getrlimit(resource.RLIMIT_NPROC),
                "nproc_enforcement_proven": False}
    if case == "cpu":
        resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
        # Fixed, credential-free diagnostic, stopped by RLIMIT_CPU or the parent.
        while True:
            pass
    if case == "session":
        # A bounded child, not a daemon: demonstrate that process groups alone
        # are escapable. The child exits immediately; no orphan is left behind.
        result = subprocess.run([sys.executable, "-I", "-S", "-c",
                                 "import os; os.setsid(); print(os.getpgrp())"],
                                capture_output=True, text=True, timeout=3)
        return {"parent_group": os.getpgrp(), "child_returncode": result.returncode,
                "child_group": int(result.stdout) if result.returncode == 0 else None}
    raise ValueError("unknown fixed experiment")


def run_child(case, root):
    with subprocess.Popen([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                           "--child", case, "--root", str(root)],
                          env={"PATH": os.defpath, "LANG": "C.UTF-8"}, close_fds=True,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return {"returncode": process.returncode, "timed_out": timed_out,
                "result": json.loads(stdout) if process.returncode == 0 else None,
                "stderr": stderr}


def audit():
    result = {"kernel": platform.release(), "architecture": platform.machine(),
              "uid": os.getuid(), "gid": os.getgid(), "sandbox_confirmed": False,
              "task_code_executed": False}
    if sys.platform != "linux" or platform.machine() != "x86_64":
        return {**result, "unsupported": "probes require Linux x86_64"}
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    result["process_status"] = {key: status[key].strip() for key in ("CapEff", "NoNewPrivs", "Seccomp")}
    with tempfile.TemporaryDirectory(prefix="coding-kernel-probe-") as temporary:
        root = Path(temporary)
        (root / "allowed").mkdir()
        (root / "outside").mkdir()
        (root / "allowed/own").write_bytes(b"public fixture")
        (root / "outside/canary").write_bytes(b"synthetic canary; not a secret")
        (root / "allowed/link").symlink_to(root / "outside/canary")
        (root / "outside/link").symlink_to("synthetic-target-name")
        result["experiments"] = {case: run_child(case, root)
                                 for case in (*NAMESPACES, "uid", "chroot", "landlock", "seccomp", "combined",
                                              "rlimits", "nproc", "cpu", "session")}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=[*NAMESPACES, "uid", "chroot", "landlock", "seccomp", "combined",
                                           "rlimits", "nproc", "cpu", "session"])
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.child:
        if sys.platform != "linux" or platform.machine() != "x86_64" or args.root is None:
            parser.error("fixed child experiments require Linux x86_64 and a synthetic root")
        result = child(args.child, args.root)
    else:
        result = audit()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
