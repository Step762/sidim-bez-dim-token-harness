"""Fixed evaluation matrix and 20 predeclared qualification pairs per mode."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

if __package__:
    from .evaluate import DATASET, evaluate, load_dataset, save, sha, runtime_fingerprint
else:
    from evaluate import DATASET, evaluate, load_dataset, save, sha, runtime_fingerprint


def run_suite(output, *, dataset=DATASET, resume=False):
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    manifest, references = load_dataset(dataset)
    matrix = [("A-local","local","priority",0),
              ("B-unavailable","unavailable","priority",100000),
              ("C-good","mock-good","priority",100000),
              ("C-wrong","mock-wrong","priority",100000),
              ("C-timeout","mock-timeout","priority",100000),
              ("C-no-usage","mock-no-usage","priority",100000),
              *[(f"D-{strategy}","mock-good",strategy,1000) for strategy in ("priority","original","reward")],
              *[(f"D-scarce-{strategy}","mock-good",strategy,500) for strategy in ("priority","original","reward")]]

    def run(name,mode,strategy,budget,ids=None):
        path = output/name
        if path.with_suffix(".json").exists():
            if not resume:
                raise ValueError("report exists; use a fresh directory or --resume")
            report = json.loads(path.with_suffix(".json").read_text())
            expected_ids = sorted(ids or references)
            if (report["dataset_manifest_sha256"] != sha(Path(dataset)/"manifest.json")
                or report["reference_sha256"] != sha(Path(dataset)/"references.json")
                or report["mock_sha256"] != sha(Path(dataset)/"mocks.json")
                or report["runtime_sha256"] != runtime_fingerprint()
                or report["comparison_policy_version"] != "rational-canonical-json-v1"
                or [row["task_id"] for row in report["rows"]] != expected_ids
                or (report["mode"],report["strategy"],report["budget"]) != (mode,strategy,budget)):
                raise ValueError("saved report does not match this experiment")
        else:
            print("Running",name,flush=True)
            report = evaluate(dataset,mode=mode,strategy=strategy,budget=budget,task_ids=ids)
            save(report,path)
        print(name,json.dumps(report["summary"],sort_keys=True),flush=True)
        return report

    reports = {name:run(name,mode,strategy,budget) for name,mode,strategy,budget in matrix}
    pairs = []
    for mode in ("local","mock-good"):
        for pair in manifest["pairs"]:
            assert all(references[key]["answerable"] for key in pair["tasks"])
            report = run(f"pairs/{mode}-{pair['pair_id']}",mode,"priority",0 if mode=="local" else 1000,pair["tasks"])
            pairs.append({**pair,"mode":mode,"correct":report["summary"]["correct"],
                          "answers":{row["task_id"]:{"status":row["status"],"actual":row["actual"],
                                     "outcome":row["outcome"],"family":row["family"]} for row in report["rows"]},
                          "llm_spent":report["summary"]["llm_spent"],"seconds":report["wall_seconds"]})
    pair_groups = {}
    for mode in ("local","mock-good"):
        pair_groups[mode] = {}
        for category in sorted({pair["category"] for pair in pairs}):
            group = [pair for pair in pairs if pair["mode"]==mode and pair["category"]==category]
            counts = Counter(pair["correct"] for pair in group)
            pair_groups[mode][category] = {"pairs":len(group),"2/2":counts[2],"1/2":counts[1],"0/2":counts[0]}
    overview = {"dataset_version":manifest["version"],"dataset_manifest_sha256":sha(Path(dataset)/"manifest.json"),
                "runtime_sha256":runtime_fingerprint(),"comparison_policy_version":"rational-canonical-json-v1",
                "results":{name:{key:report[key] for key in ("summary","by_type","by_split","by_difficulty",
                             "budget_state","wall_seconds","agent_seconds")} for name,report in reports.items()},
                "qualification":pair_groups,"pairs":pairs,
                "scope":"Synthetic diagnostic distribution; mock outcomes are not real LLM accuracy.",
                "real_provider_calls":0,"trusted_code":False}
    (output/"overview.json").write_text(json.dumps(overview,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
    lines = ["# V2 №5: воспроизводимая матрица", "",
             "128 синтетических задач; результаты не являются прогнозом конкурсных баллов.","",
             "| Режим | Верно | Неверно | Пропущено | Баллы | Вызовы mock HTTP | Учтённый расход | Резерв | Wall, с |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name,report in reports.items():
        row = report["summary"]
        lines.append(f"| {name} | {row['correct']} | {row['wrong']} | {row['skipped']} | {row['reward']}/{row['possible_reward']} | {row['llm_calls']} | {row['llm_spent']} | {row['llm_reserved']} | {report['wall_seconds']:.3f} |")
    lines += ["","При неизвестном usage учтённый расход — нижняя граница, не доказательство бесплатного вызова.","",
              "## Отборочные пары","","| Режим | Категория | 2/2 | 1/2 | 0/2 |","| --- | --- | ---: | ---: | ---: |"]
    for mode,groups in pair_groups.items():
        for category,counts in groups.items():
            lines.append(f"| {mode} | {category} | {counts['2/2']} | {counts['1/2']} | {counts['0/2']} |")
    lines += ["","Каждая из 20 пар запущена отдельно в каждом режиме. В парах только задачи с определённым эталоном.",
              "2/2 на этих парах не гарантирует прохождения отбора организаторов. Подробности и причины находятся в JSON и отдельных отчётах."]
    (output/"overview.md").write_text("\n".join(lines)+"\n")
    return overview


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--dataset",type=Path,default=DATASET)
    parser.add_argument("--resume",action="store_true")
    args = parser.parse_args()
    run_suite(args.output_dir,dataset=args.dataset,resume=args.resume)
