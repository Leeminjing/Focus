"""本文件对外提供已绑定缓冲式 TAP 报告的领域证据回归。
输入为完整嵌套计划、真实执行证明与失败/截断/敏感标题变体；输出为命名叶用例、suite 归属和可靠统计或 unknown。
具体工作流为调用生产 TestResultParser，核对缺少可选 Node 诊断的可信 TAP 仍完整，错误计划和未闭合 suite 拒绝覆盖。
示例：pytest backend/tests/test_buffered_tap_evidence.py。
"""

import json

import pytest

from backend.app.desktop.domain_evidence.tests import TestResultParser


TAP = '''TAP version 13
1..1
ok 1 - tests/config.test.ts # time=9.10ms {
    1..1
    ok 1 - configuration # time=8.20ms {
        1..2
        ok 1 - persists prompt # time=1.20ms
        ok 2 - optional path # SKIP
    }
}
'''


def _parse(output=TAP, **changes):
    execution = {'run_id':'run','call_id':'call','status':'exited','exit_code':0,'workspace':'.','output':output,**changes}
    return TestResultParser.parse('powershell',json.dumps(execution),command='npm test -- --reporter=tap',run_id='run',call_id='call')


def test_buffered_tap_without_node_diagnostics_has_real_named_leaf_results():
    parsed=_parse()
    assert parsed['status']=='verified'
    assert parsed['metrics']=={'passed':1,'failed':0,'skipped':1,'count_status':'exact'}
    report=parsed['named_results']
    assert report['status']=='complete'
    assert [(c['name'],c['status']) for c in report['cases']]==[('persists prompt','passed'),('optional path','skipped')]
    assert report['cases'][0]['suite']==['tests/config.test.ts','configuration']
    assert len(report['suites'])==2
    assert all('time=' not in str(c) for c in report['cases']+report['suites'])


@pytest.mark.parametrize('output',[
    TAP.rsplit('}',1)[0],TAP.replace('1..2','1..3'),TAP.replace('ok 2 - optional','ok 1 - optional'),
    TAP+'1..1\n',TAP.replace('ok 1 - persists','not ok 1 - persists'),TAP+'Bail out!\n',
],ids=['unclosed_suite','nested_plan','duplicate_point','duplicate_root_plan','contradictory_parent','bailout'])
def test_malformed_or_inconsistent_buffered_tap_has_no_named_coverage(output):
    result=_parse(output)
    assert result['named_results']['status']=='unknown'
    assert result['named_results']['cases']==[]


def test_failed_or_truncated_buffered_execution_cannot_verify():
    failed=TAP.replace('ok 1 -','not ok 1 -')
    assert _parse(failed,exit_code=1)['status']=='failed'
    truncated=_parse(truncated=True)
    assert truncated['named_results']['status']=='unknown'
    assert truncated['status']!='verified'


@pytest.mark.parametrize('diagnostics', ['# tests 3\n', '# pass 0\n', '# suites 1\n', '# pass 1\n# pass 1\n'])
def test_optional_diagnostics_must_agree_with_actual_tree(diagnostics):
    parsed=_parse(TAP+diagnostics)
    report=parsed['named_results']
    assert report['status']=='unknown'
    assert report['cases']==[]
    assert parsed['status']!='verified'


def test_agreeing_optional_diagnostics_and_sensitive_titles():
    assert _parse(TAP+'# tests 2\n# suites 2\n# pass 1\n# skipped 1\n# fail 0\n')['status']=='verified'
    report=_parse(TAP.replace('configuration', 'api_key=sk-123456789abcdef'))['named_results']
    assert report['status']=='complete'
    assert all(case['coverage']=='unknown' for case in report['cases'])
    assert 'sk-123456789abcdef' not in json.dumps(report)


def test_truncated_tap_with_diagnostics_cannot_publish_verified_aggregate():
    parsed=_parse(TAP+'# tests 2\n# pass 1\n# fail 0\n# skipped 1\n',truncated=True)
    assert parsed['status']=='unknown'
    assert parsed['metrics']['count_status']=='unknown'


@pytest.mark.parametrize('changes',[{'exit_code':1},{'status':'cancelled'},{'run_id':'other'}])
def test_incomplete_tap_preserves_real_execution_failure(changes):
    parsed=_parse(TAP+'# pass 1\n# pass 1\n',**changes)
    assert parsed['status']=='failed'
    assert parsed['metrics']['count_status']=='unknown'
