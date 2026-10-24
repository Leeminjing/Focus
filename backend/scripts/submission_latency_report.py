"""Aggregate the opt-in submission benchmark's raw samples without inventing data.

Usage: python -m backend.scripts.submission_latency_report REPORT_DIRECTORY
"""

import argparse
import json
from pathlib import Path

from backend.app.desktop.patrol.agent_loop.diagnostics.submission_latency import SCENARIOS, SubmissionSample, summarize


def write_report(directory: Path):
    samples, sources = [], []
    for scenario in SCENARIOS:
        for path in sorted(directory.glob(f"{scenario}-*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            raw = document["sample"]
            if raw["scenario"] != scenario:
                raise ValueError(f"scenario mismatch in {path.name}")
            # Earlier observer samples did not retain the baseline artifact IDs.
            # Preserve them honestly instead of guessing which warm stage ran
            # before submission from duration or order alone.
            for stage in raw["stages"]:
                stage.setdefault("phase", "unclassified" if document["metadata"].get("preparation") else "measurement")
            observed_modes = sorted({attempt["mode"] for stage in raw["stages"]
                                     if stage["phase"] == "measurement" for attempt in stage.get("attempts", ())
                                     if attempt.get("role") == "semantic_index_build" and "mode" in attempt})
            expected_mode = {"exact_cache": "exact_hit", "small_append": "incremental"}.get(scenario, "full")
            document["metadata"]["index_mode_check"] = {"expected": expected_mode, "observed": observed_modes,
                "expected_mode_observed_any_revision": expected_mode in observed_modes if observed_modes else None,
                "scope": "Sample-level diagnostics; exported stage attempts do not bind modes to source_revision_id."}
            samples.append(SubmissionSample(**{key: raw[key] for key in SubmissionSample.__dataclass_fields__ if key in raw}))
            sources.append({"file": path.name, **document["metadata"]})
    if not samples:
        raise ValueError("no submission benchmark samples found")
    if len({(s.scenario, s.repeat) for s in samples}) != len(samples):
        raise ValueError("duplicate scenario/repeat")
    observer_versions = {s.get("observer_version", 1) for s in sources}
    configuration_keys = ("model", "budget_preset", "curation_max_output_tokens", "derivation_max_output_tokens", "budgets", "run_scope")
    configurations = {json.dumps({k: source.get(k) for k in configuration_keys}, sort_keys=True) for source in sources}
    if len(observer_versions) > 1 or len(configurations) > 1:
        raise ValueError("mixed observers or model/budget configurations must be reported separately")
    report = {**summarize(samples), "sources": sources,
              "scope": "Synthetic full-pipeline smoke, default model/normal budget; no production caches or histories.",
              "timing_limitations": "HTTP uses in-process ASGI transport. Downstream times include supervisor startup, recovery and DB polling. Preparation is excluded but charged to the same normal budget. Not desktop click-to-render latency.",
              "observer_versions": sorted(observer_versions),
              "attribution_limitations": ("Observer v2 follows pending/deferred causal_chain inputs and derived-Context Runs only. It cannot recover later Mission-source-only decisions or Primary continuation Run timings. The updated v3 probe adds those paths; do not mix v2 samples with the v3 all-Run scope." if observer_versions == {2} else None),
              "legacy_observer_limitations": ("Acceptance includes supervisor start; warm stage diagnostics are unclassified. Do not sum stages into wall time." if observer_versions == {1} else None),
              "configuration_limitations": "The configured default model is retained. The curation output cap uses its product schema default (8192), replacing the host's 65536 in test memory only; host defaults are not measured unchanged."}
    (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    scenario_names = {"cold": "冷构建", "exact_cache": "精确缓存", "small_append": "少量追加", "long_tool_output": "长工具输出"}
    metric_names = {"durable_acceptance": "耐久受理", "patrol_decision_committed": "Patrol 决策提交",
                    "derived_context_available": "派生 Context 可用", "first_run_started": "首个关联 Run 开始",
                    "first_effective_output": "首个关联 Run 有效输出"}
    lines = ["# 提交到可执行：观测结果", "", f"Observer：{sorted(observer_versions)}。所有累计耗时单位为秒。",
             "", "P50／P95 只针对已观测到的里程碑；未观测到不计为 0。每类 3 次不足以解释 P95，不能用于选择分段参数。",
             "", "| 场景 | 样本 | 五项全部到达 | 结束状态 |", "| --- | ---: | ---: | --- |"]
    for scenario, result in report["scenarios"].items():
        outcomes = ", ".join(f"{key}={value}" for key, value in result["outcomes"].items()) or "—"
        lines.append(f"| {scenario_names[scenario]} | {result['samples']} | {result['completed']} | {outcomes} |")
    lines += ["", "| 场景 | 指标 | 观测／样本 | P50（秒） | P95（秒） |", "| --- | --- | ---: | ---: | ---: |"]
    def seconds(value):
        return "—" if value is None else f"{value / 1000:.3f}"
    for scenario, result in report["scenarios"].items():
        for name, metric in result["metrics"].items():
            lines.append(f"| {scenario_names[scenario]} | {metric_names[name]} | {metric['observed']}/{result['samples']} | {seconds(metric['p50_ms'])} | {seconds(metric['p95_ms'])} |")
    lines += ["", "| 场景 | 实际索引模式（样本数） | 阶段阻断代码（样本数） |",
              "| --- | --- | --- |"]
    for scenario in SCENARIOS:
        modes, blockers = {}, {}
        for sample, source in zip(samples, sources):
            if sample.scenario != scenario:
                continue
            for mode in source["index_mode_check"]["observed"]:
                modes[mode] = modes.get(mode, 0) + 1
            sample_blockers = {f"{stage['stage']}/{stage['failure_code']}" for stage in sample.stages
                               if stage["phase"] == "measurement" and stage.get("failure_code")}
            for blocker in sample_blockers:
                blockers[blocker] = blockers.get(blocker, 0) + 1
        def counts(values):
            return ", ".join(f"`{key}`={value}" for key, value in sorted(values.items())) or "—"
        lines.append(f"| {scenario_names[scenario]} | {counts(modes)} | {counts(blockers)} |")
    lines += ["", "准备阶段与提交后的时间分开；并发／重复投影的阶段数据不可相加替代累计耗时。",
              "索引模式是整个样本的测量阶段诊断；导出的阶段未将模式绑定到目标 source_revision_id，因此不能单凭模式表验证目标历史的缓存路径。",
              "阶段阻断按样本去重；一次样本可能出现多个代码。未派生可能是策略选择或阶段失败，不能全部解读为耗时超标。",
              "", "配置、原始样本、缓存模式及用量见 [summary.json](summary.json)，测量边界与复现见 [README.md](README.md)。"]
    if report["attribution_limitations"]:
        lines += ["", report["attribution_limitations"]]
    (directory / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    report = write_report(args.directory)
    print(json.dumps(report["scenarios"], ensure_ascii=False, indent=2))
