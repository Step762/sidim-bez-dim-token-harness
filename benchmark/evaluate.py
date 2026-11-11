"""Independent benchmark grader and local mock proxy; never imported by agent."""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from fractions import Fraction
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset"
MODES = ("local", "unavailable", "mock-good", "mock-wrong", "mock-timeout", "mock-no-usage")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_fingerprint(root=ROOT):
    inputs = [line[1:] for line in (root/".dockerignore").read_text().splitlines() if line.startswith("!")]
    return {name:sha(root/name) for name in inputs}


def load_dataset(directory):
    directory = Path(directory)
    manifest = json.loads((directory/"manifest.json").read_text())
    references = json.loads((directory/"references.json").read_text())
    actual_files = {str(path.relative_to(directory)) for path in (directory/"tasks").rglob("*") if path.is_file()}
    if actual_files != set(manifest["files"]):
        raise ValueError("dataset inputs differ from the frozen manifest")
    for relative, expected in manifest["files"].items():
        path = directory/relative
        if path.is_symlink() or sha(path) != expected:
            raise ValueError("dataset file is linked or changed: " + relative)
    for task_id in references:
        task = json.loads((directory/"tasks"/task_id/"task.json").read_text())
        if set(task) != {"task_id","task_type","instruction","reward"}:
            raise ValueError("reference or unexpected fields in task input")
        ref = references[task_id]
        if (task["task_id"]!=task_id or ref["task_id"]!=task_id or task["reward"]!=ref["reward"]
            or task["task_type"]!=ref["task_type"] or ref["answerable"]!=(ref["answer"] is not None)
            or ref["policy"] not in {"numeric","exact","json","coding"}):
            raise ValueError("reference metadata does not match task")
    return manifest, references


def compare(actual, reference):
    """Explicit benchmark policy, not a claim about the hidden contest grader."""
    if actual is None:
        return "skipped"
    if not reference["answerable"]:
        return "wrong"
    policy = reference["policy"]
    expected = reference["answer"]
    if policy == "coding":
        # Never execute generated/modified task Python on the host to grade DONE.
        return "unverified" if actual.strip() == "DONE" else "wrong"
    if policy == "numeric":
        try:
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:/[+-]?\d+)?", actual.strip()):
                return "wrong"
            equal = Fraction(actual.strip()) == Fraction(expected)
        except (ValueError, ZeroDivisionError):
            equal = False
    elif policy == "json":
        try:
            # Duplicate keys must not silently erase an incorrect value.
            def unique(items):
                out = {}
                for key,value in items:
                    if key in out:
                        raise ValueError("duplicate JSON key")
                    out[key] = value
                return out
            # Canonical JSON preserves bool/number and integer/float spellings;
            # Python's ordinary equality would incorrectly accept true == 1.
            canonical = lambda value:json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False)
            equal = canonical(json.loads(actual,object_pairs_hook=unique)) == canonical(json.loads(expected))
        except (ValueError,RecursionError):
            equal = False
    else:
        equal = actual.strip() == expected
    return "correct" if equal else "wrong"


@contextmanager
def mock_proxy(mode, fixtures):
    """Real loopback HTTP, handcrafted plans; no reference-answer lookup."""
    state = {"requests":[]}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.loads(body["messages"][-1]["content"])
            key = hashlib.sha256(payload["instruction"].encode()).hexdigest()
            fixture = fixtures.get(key)
            def reference_field(value):
                if isinstance(value,dict):
                    return bool(set(value)&{"expected","check","oracle","hidden_examples"}) or any(reference_field(v) for v in value.values())
                return isinstance(value,list) and any(reference_field(v) for v in value)
            event = {"task_id":fixture["task_id"] if fixture else None,
                     "instruction_hash":key,"mode":mode,"payload_keys":sorted(payload),
                     "reference_fields_present":reference_field(payload)}
            state["requests"].append(event)
            if mode == "unavailable":
                self.send_response(503)
                self.end_headers()
                return
            plan = fixture["plan"] if fixture else {"status":"unsolvable"}
            if mode == "mock-wrong":
                # Wrong but well-formed proposal, not a forged correct answer.
                plan = {"entity":"unrelated entity","relations":[],"field":"born","answer":"9999"}
                if payload["type"] == "math":
                    plan = {"initial":{"value":"9999","quote":"invented"},"steps":[]}
                if payload["type"] == "data_analysis":
                    plan = {"file":"observations.csv","operation":"count","column":None,"filters":[]}
            content = json.dumps(plan,ensure_ascii=False)
            # Deterministic SYNTHETIC counts, not measurements of model tokens.
            prompt = 64 + len(body["messages"][-1]["content"].encode())//4
            completion = min(256,16+len(content.encode())//3)
            usage = {"prompt_tokens":prompt,"completion_tokens":completion,"total_tokens":prompt+completion}
            event.update({"usage":None if mode in {"mock-timeout","mock-no-usage"} else usage,
                          "fixture_available":fixture is not None})
            response = {"id":"benchmark-mock","object":"chat.completion","model":body["model"],
                        "choices":[{"index":0,"message":{"role":"assistant","content":content},"finish_reason":"stop"}]}
            if mode != "mock-no-usage":
                response["usage"] = usage
            encoded = json.dumps(response).encode()
            if mode == "mock-timeout":
                time.sleep(0.25)
            try:
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError,ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda:server.serve_forever(poll_interval=0.02),daemon=True)
    thread.start()
    state["endpoint"] = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def execute(command, *, environment=None, timeout=180):
    with subprocess.Popen(command,cwd=ROOT,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True,start_new_session=True) as process:
        try:
            stdout,stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL)
            stdout,stderr = process.communicate()
            raise RuntimeError("benchmark worker exceeded its timeout") from None
        if process.returncode:
            raise RuntimeError(f"benchmark worker failed ({process.returncode}): {stderr[-4000:]}")
    return stdout,stderr


def summary(rows):
    total = len(rows)
    counts = Counter(row["status"] for row in rows)
    emitted = sum(row["actual"] is not None for row in rows)
    impossible = sum(not row["answerable"] for row in rows)
    honest = sum(row["status"]=="skipped" and not row["answerable"] for row in rows)
    spent = sum(Fraction(row["llm_spent"]) for row in rows)
    earned = sum(row["reward"] for row in rows if row["status"]=="correct")
    paid_reward = sum(row["reward"] for row in rows if row["status"]=="correct" and row["llm_calls"])
    return {"total":total,"correct":counts["correct"],"wrong":counts["wrong"],"skipped":counts["skipped"],
            "unverified":counts["unverified"],"answerable":sum(row["answerable"] for row in rows),
            "reward":earned,"possible_reward":sum(row["reward"] for row in rows if row["answerable"]),
            "seconds":sum(row["seconds"] for row in rows),"llm_calls":sum(row["llm_calls"] for row in rows),
            "llm_spent":float(spent),"llm_reserved":float(sum((Fraction(row["llm_reserved"]) for row in rows),Fraction())),
            "false_positive_fraction":counts["wrong"]/emitted if emitted else None,
            "answer_precision":counts["correct"]/emitted if emitted else None,
            "skip_fraction":counts["skipped"]/total if total else None,
            "justified_refusals":honest,"unanswerable":impossible,
            "justified_refusal_recall":honest/impossible if impossible else None,
            "false_negative_skips":sum(row["status"]=="skipped" and row["answerable"] for row in rows),
            "paid_correct_reward_per_unit":float(Fraction(paid_reward)/spent) if spent else None}


def grouped(rows,key):
    return {value:summary([row for row in rows if row[key]==value]) for value in sorted({row[key] for row in rows})}


def evaluate(dataset=DATASET, *, mode="local", strategy="priority", budget=None, task_ids=None,
             split="all", backend="host", image="sidim-bez-dim:harness-v2-math"):
    dataset = Path(dataset)
    manifest,references = load_dataset(dataset)
    if mode not in MODES:
        raise ValueError("unknown benchmark mode")
    if backend == "docker" and mode != "local":
        raise ValueError("Docker benchmark currently verifies only local mode; mock proxy is host loopback only")
    selected = sorted(task_ids if task_ids is not None else
                      [key for key,value in references.items() if split=="all" or value["split"]==split])
    if not selected or len(selected)!=len(set(selected)) or any(key not in references for key in selected):
        raise ValueError("selection must contain known unique task IDs")
    budget = (0 if mode=="local" else 100000) if budget is None else budget
    if mode == "local" and budget != 0:
        raise ValueError("fully local mode requires BUDGET=0")
    fixtures = json.loads((dataset/"mocks.json").read_text())
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="benchmark-run-") as temporary:
        base = Path(temporary)
        tasks = base/"tasks"
        tasks.mkdir()
        output = base/"output"
        output.mkdir()
        for key in selected:
            shutil.copytree(dataset/"tasks"/key,tasks/key)
        before = {str(path.relative_to(tasks)):sha(path) for path in tasks.rglob("*") if path.is_file()}
        trace_path = output/"trace.json"
        arguments = ["--tasks-dir",str(tasks),"--result",str(trace_path),"--budget",str(budget),"--strategy",strategy]
        environment = {"PATH":os.defpath,"LANG":"C.UTF-8","PYTHONDONTWRITEBYTECODE":"1","BUDGET":str(budget)}
        if backend == "docker":
            # Only task INPUTS, worker code and an empty trace directory are mounted.
            # References, mock fixtures, corpus, Git and agent secrets are absent.
            for path in [base,tasks,output,*tasks.rglob("*")]:
                path.chmod(0o777 if path.is_dir() else 0o666)
            docker_args = ["docker","run","--rm","--pull=never","--network","none","--read-only",
                          "--tmpfs","/tmp:rw,size=2g","--cap-drop","ALL","--security-opt","no-new-privileges",
                          "--pids-limit","512","--memory","16g","--cpus","8","-e","HOME=/tmp","-e","BUDGET=0",
                          "--mount",f"type=bind,source={tasks},target=/data/tasks",
                          "--mount",f"type=bind,source={output},target=/audit-result",
                          "--mount",f"type=bind,source={HERE/'worker.py'},target=/audit/worker.py,readonly",
                          "--workdir","/app","--entrypoint","python",image,"-B","/audit/worker.py",
                          "--runtime-dir","/app","--tasks-dir","/data/tasks","--result","/audit-result/trace.json",
                          "--budget","0","--strategy",strategy]
            stdout,stderr = execute(docker_args)
            requests = []
        elif mode == "local":
            stdout,stderr = execute([sys.executable,"-B",str(HERE/"worker.py"),*arguments],environment=environment)
            requests = []
        else:
            with mock_proxy(mode,fixtures) as proxy:
                stdout,stderr = execute([sys.executable,"-B",str(HERE/"worker.py"),*arguments,
                                          "--endpoint",proxy["endpoint"],"--llm-timeout",
                                          "0.08" if mode=="mock-timeout" else "2.0"],environment=environment)
                requests = list(proxy["requests"])
        trace = json.loads(trace_path.read_text())
        outcomes = {Path(key).name:value for key,value in trace["planner"]["outcomes"].items()}
        costs = {Path(key).name:value for key,value in trace["planner"]["budget"]["tasks"].items()}
        rows = []
        for key in selected:
            ref = references[key]
            path = tasks/key/"answer.txt"
            actual = path.read_text().strip() if path.exists() else None
            observed = trace["observations"].get(key,[])
            cost = costs.get(key,{})
            row = {**ref,"actual":actual,"status":compare(actual,ref),
                   "seconds":sum(part["seconds"] for part in observed),"outcome":outcomes[key]["outcome"],
                   "observations":observed,"llm_calls":cost.get("calls",0),
                   "llm_spent":str(Fraction(cost.get("spent_tenths",0),10)),
                   "llm_reserved":str(Fraction(cost.get("reserved_tenths",0),10)),
                   "answer_mode":stat.S_IMODE(path.stat().st_mode) if path.exists() else None}
            rows.append(row)
        changed_inputs = [relative for relative,old in before.items() if sha(tasks/relative)!=old]
        if changed_inputs:
            raise RuntimeError("safe benchmark unexpectedly modified input files")
    result = {"dataset_version":manifest["version"],"dataset_manifest_sha256":sha(dataset/"manifest.json"),
              "reference_sha256":sha(dataset/"references.json"),"mock_sha256":sha(dataset/"mocks.json"),
              "runtime_sha256":runtime_fingerprint(),"comparison_policy_version":"rational-canonical-json-v1",
              "mode":mode,"strategy":strategy,"backend":backend,"budget":budget,"split":split,
              "execution":"real agent, no LLM" if mode=="local" else "real agent, controlled local HTTP simulation",
              "wall_seconds":time.perf_counter()-started,"agent_seconds":trace["agent_seconds"],
              "summary":summary(rows),"by_type":grouped(rows,"task_type"),"by_split":grouped(rows,"split"),
              "by_difficulty":grouped(rows,"difficulty"),"budget_state":trace["planner"]["budget"],
              "order":[[Path(key).name,phase] for key,phase in trace["planner"]["order"]],
              "mock_requests":requests,"rows":rows,"agent_stdout":stdout,"agent_stderr":stderr,
              "changed_inputs":changed_inputs,"trusted_code":False}
    return result


def markdown(report):
    lines = [f"# Benchmark {report['dataset_version']}","",
             f"Режим: `{report['mode']}`; backend: `{report['backend']}`; стратегия: `{report['strategy']}`; BUDGET={report['budget']}.",
             f"Выполнение: {report['execution']}. Недоверенный coding выключен.","",
             "| Тип | Всего | Верно | Неверно | Пропущено | Баллы / доступные | Время узлов, с | LLM, условные ед. |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for kind,row in report["by_type"].items():
        lines.append(f"| {kind} | {row['total']} | {row['correct']} | {row['wrong']} | {row['skipped']} | {row['reward']}/{row['possible_reward']} | {row['seconds']:.4f} | {row['llm_spent']} |")
    lines += ["",f"Wall-clock: {report['wall_seconds']:.3f} с; собственно агент: {report['agent_seconds']:.3f} с.",
              "Время по типам — сумма измерений workflow.invoke; загрузка, preflight и публикация входят только в общее время.",
              f"Budget state: `{json.dumps(report['budget_state'],ensure_ascii=False)}`.","",
              "LLM usage здесь синтетическая статистика мока. При timeout/ошибке/нет usage известный расход — лишь нижняя граница; резерв не объявляется потраченным.",
              "Числовая политика benchmark: точное равенство рациональных чисел (1/2 = 0.5). Текст: точное совпадение после внешнего whitespace. JSON: равенство структуры без повторов ключей.",
              "Конкурсная совместимость нецелых чисел и структурированных ответов неизвестна; такие строки помечены отдельно. DONE без независимой проверки кода получает unverified и 0 баллов.","",
              "## Development / holdout","","| Набор | Верно | Неверно | Пропущено | Баллы |","| --- | ---: | ---: | ---: | ---: |"]
    for split,row in report["by_split"].items():
        lines.append(f"| {split} | {row['correct']} | {row['wrong']} | {row['skipped']} | {row['reward']}/{row['possible_reward']} |")
    lines += ["","## Все результаты","","| ID | Сложность | Статус | Ответ | Эталон | Причина |","| --- | --- | --- | --- | --- | --- |"]
    def cell(value):
        return str(value).replace("|","\\|").replace("\n"," ")
    for row in report["rows"]:
        why = row["observations"][-1]["reason"] if row["observations"] else row["outcome"]
        lines.append("| " + " | ".join(cell(v) for v in (row["task_id"],row["difficulty"],row["status"],row["actual"],row["answer"],why)) + " |")
    return "\n".join(lines)+"\n"


def save(report,destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    # A run must not silently overwrite an existing report.
    for suffix in (".json",".md"):
        if destination.with_suffix(suffix).exists():
            raise ValueError("choose a new output path; report already exists")
    destination.with_suffix(".json").write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
    destination.with_suffix(".md").write_text(markdown(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset",type=Path,default=DATASET)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--mode",choices=MODES,default="local")
    parser.add_argument("--strategy",choices=("priority","original","reward"),default="priority")
    parser.add_argument("--budget",type=int)
    parser.add_argument("--split",choices=("all","development","holdout"),default="all")
    parser.add_argument("--task-id",action="append",dest="task_ids")
    parser.add_argument("--backend",choices=("host","docker"),default="host")
    parser.add_argument("--image",default="sidim-bez-dim:harness-v2-math")
    args = parser.parse_args()
    report = evaluate(args.dataset,mode=args.mode,strategy=args.strategy,budget=args.budget,
                      task_ids=args.task_ids,split=args.split,backend=args.backend,image=args.image)
    save(report,args.output)
    print(json.dumps({"summary":report["summary"],"output":str(args.output)},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
