"""本文件对外提供 parse_named_test_report 的可信 TAP 命名结果投影。

输入为已绑定且正常退出的工具报告文本和实际敏感值；输出为报告哈希、可靠的 suite/case 身份及状态或 unknown。
具体工作流为按流式完成顺序或缓冲式 suite/计划/闭合树恢复叶用例；逐层检查编号、数量、闭合及父子状态，现有 Node 汇总存在时仍核对。
缓冲式可信报告不依赖 Node 专有诊断；敏感标题仅发布不可逆身份及未知覆盖，不从总数量生成名字。
不保存原始 stdout 或按标题推断 Mission 满足。示例：parse_named_test_report(tap, trusted=True, secrets=keys)。
"""

import re

from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.secret_redaction import redact_text

_RESULT = re.compile(r"^( *)(not ok|ok) (\d+)(?: -)? (.+?)\s*$", re.M)
_SECRET = re.compile(r"(?:sk-[\w-]{8,}|(?:api[_ -]?key|authorization|bearer|password|secret)\s*[:=]\s*\S+)", re.I)


def _safe_title(title, secrets):
    return title if redact_text(title, secrets) == title and not _SECRET.search(title) else None


def _cases(rows, report_hash, secrets):
    ancestors, cases, suites = [], [], []
    for index in range(len(rows) - 1, -1, -1):
        depth, status, number, title = rows[index]
        while ancestors and ancestors[-1][0] >= depth:
            ancestors.pop()
        path = [t for _, t in ancestors]
        identity = canonical_hash([report_hash, path, depth, number, title])
        name = _safe_title(title, secrets)
        safe_path = [_safe_title(t, secrets) for t in path]
        item = {"case_id": identity, "name": name, "suite": safe_path, "status": status,
                "coverage": "available" if name is not None and None not in safe_path else "unknown"}
        if index and rows[index - 1][0] > depth:
            suites.append(item)
            ancestors.append((depth, title))
        else:
            cases.append(item)
    return list(reversed(cases)), list(reversed(suites))


def _buffered_item(path, number, title, status, report_hash, secrets):
    name = _safe_title(title, secrets)
    safe_path = [_safe_title(t, secrets) for t in path]
    return {'case_id': canonical_hash(['buffered-tap-v1', report_hash, path, number, title]),
            'name': name, 'suite': safe_path, 'status': status,
            'coverage': 'available' if name is not None and None not in safe_path else 'unknown'}


def _buffered_group(tokens, index, depth, path, report_hash, secrets):
    plan = tokens[index]
    if plan[0] != 'plan' or plan[1] != depth:
        raise ValueError('missing TAP plan')
    index += 1
    cases, suites = [], []
    for number in range(1, plan[2] + 1):
        row = tokens[index]
        if row[0] != 'point' or row[1] != depth or row[3] != number:
            raise ValueError('TAP point numbering')
        raw = row[4]
        buffered = raw.endswith(' {')
        title, _, directive = (raw[:-2] if buffered else raw).partition(' # ')
        status = 'skipped' if re.match(r'(?:SKIP|TODO)\b', directive, re.I) else 'failed' if row[2] == 'not ok' else 'passed'
        item = _buffered_item(path, number, title, status, report_hash, secrets)
        index += 1
        if not buffered:
            cases.append(item)
            continue
        child_depth = tokens[index][1]
        if child_depth <= depth:
            raise ValueError('TAP child indentation')
        children, nested, index = _buffered_group(tokens, index, child_depth, [*path, title], report_hash, secrets)
        if tokens[index] != ('end', depth):
            raise ValueError('TAP suite closure')
        if status == 'passed' and any(c['status'] == 'failed' for c in [*children, *nested]):
            raise ValueError('TAP parent status')
        if status == 'skipped' and any(c['status'] != 'skipped' for c in [*children, *nested]):
            raise ValueError('TAP skipped suite')
        cases.extend(children)
        suites.extend([item, *nested])
        index += 1
    return cases, suites, index


def _buffered_cases(output, report_hash, secrets):
    tokens = []
    diagnostic = False
    for line in output.splitlines():
        if line.strip() == '---':
            diagnostic = True
        elif line.strip() == '...':
            diagnostic = False
        elif not diagnostic:
            plan = re.fullmatch(r'( *)1\.\.(\d+)\s*', line)
            point = _RESULT.fullmatch(line)
            end = re.fullmatch(r'( *)}\s*', line)
            if plan:
                tokens.append(('plan', len(plan[1]), int(plan[2])))
            elif point:
                tokens.append(('point', len(point[1]), point[2], int(point[3]), point[4]))
            elif end:
                tokens.append(('end', len(end[1])))
    if not tokens or tokens[0][0] != 'plan':
        return None
    cases, suites, index = _buffered_group(tokens, 0, 0, [], report_hash, secrets)
    if index != len(tokens) or diagnostic:
        raise ValueError('TAP trailing or incomplete data')
    return cases, suites


def _totals_match(output, cases, suites, *, required):
    rows = re.findall(r"^# (tests|suites|pass|fail|skipped|cancelled) (\d+)\s*$", output, re.M)
    totals = dict(rows)
    if len(rows) != len(totals) or required and not {"tests", "pass", "fail", "skipped"}.issubset(totals):
        return False
    expected = {"tests": len(cases), "suites": len(suites), "pass": sum(c["status"] == "passed" for c in cases),
                "fail": sum(c["status"] == "failed" for c in cases), "skipped": sum(c["status"] == "skipped" for c in cases), "cancelled": 0}
    return all(int(value) == expected[key] for key, value in totals.items())


def parse_named_test_report(output, *, trusted, secrets=()):
    report_hash = canonical_hash(output)
    unknown = {"protocol": "tap", "report_hash": report_hash, "status": "unknown", "cases": [], "suites": []}
    if not trusted or len(re.findall(r"^TAP version \d+\s*$", output, re.M)) != 1 or "Bail out!" in output:
        return unknown
    try:
        buffered = _buffered_cases(output, report_hash, secrets)
    except (ValueError, IndexError, RecursionError):
        return unknown
    if buffered is not None:
        cases, suites = buffered
        if not _totals_match(output, cases, suites, required=False):
            return unknown
        return {**unknown, 'status': 'complete', 'cases': cases, 'suites': suites}
    rows = []
    for match in _RESULT.finditer(output):
        indent, result, number, raw_title = match.groups()
        title, _, directive = raw_title.partition(" # ")
        status = "skipped" if re.match(r"(?:SKIP|TODO)\b", directive, re.I) else "failed" if result == "not ok" else "passed"
        rows.append((len(indent), status, int(number), title))
    plans = re.findall(r"^1\.\.(\d+)\s*$", output, re.M)
    if not rows or len(plans) != 1:
        return unknown
    root = [r for r in rows if r[0] == 0]
    if [r[2] for r in root] != list(range(1, int(plans[0]) + 1)):
        return unknown
    cases, suites = _cases(rows, report_hash, secrets)
    if not _totals_match(output, cases, suites, required=True):
        return unknown
    return {"protocol": "tap", "report_hash": report_hash, "status": "complete", "cases": cases, "suites": suites}
