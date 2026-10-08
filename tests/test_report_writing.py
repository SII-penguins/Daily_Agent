from copy import deepcopy
from datetime import date

from daily_agent.models import ApprovedItem, MaterialRecord, RunStatus
from daily_agent.rendering.composition import featured_keys, paper_paragraphs
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.rendering.html import render_daily_html
from daily_agent.insights import build_daily_insights


def paper(key="one", confidence="medium"):
    material = MaterialRecord(
        key=key, source="arxiv", item_type="paper", title=key,
        url="https://example.org/paper", tags=["routing"],
        paper_document={"document_kind": "full_text"},
        reading={"complete": True, "verification": {"status": "located", "valid_fields": ["method", "limitations"]},
                 "claim_evidence": [{"field": "key_result", "evidence_kind": "simulation", "conditions": "12 比特；相对基线 A；不计训练成本"}]},
    )
    return ApprovedItem(key, "paper", key, "arxiv", material.url, {
        "problem": "重复搜索开销较大。", "method": "离线训练后生成 routing 线路。",
        "why_it_works": "通过离线学习摊销搜索成本。", "novelty_or_difference": "推理省去逐实例搜索。",
        "key_result": "在 12 比特模拟中比基线 A 快 3 倍。", "limitations": "只测试小规模模拟，不包含训练成本。",
        "possible_use_or_impact": "可尝试用于重复编译任务，尚未验证迁移收益。", "confidence": confidence,
    }, material)


def test_brief_retains_result_conditions_limitations_and_source_fields():
    item = paper(confidence="low")
    before = deepcopy(item.to_dict())
    paragraphs = paper_paragraphs(item)
    assert not featured_keys([item])
    result = next(p for p in paragraphs if "key_result" in p.field_keys)
    assert "3 倍" in result.text and "模拟" in result.text and "不计训练成本" in result.text
    assert any("只测试小规模模拟" in p.text for p in paragraphs)
    md = render_daily_markdown([item], date(2026, 9, 28), RunStatus())
    html = render_daily_html([item], date(2026, 9, 28), RunStatus())
    for p in paragraphs:
        assert p.text in md and p.text in html
    assert item.to_dict() == before
    assert md.index("不包含训练成本") < md.index("## 资料索引")


def test_featured_selection_uses_trust_and_config_and_keeps_rank_order():
    items = [paper("weak", "low"), paper("strong"), paper("other")]
    assert featured_keys(items, {"featured_papers": 1}) == {"strong"}
    assert featured_keys(items, {"featured_papers": 0}) == set()
    items[1].material.reading["complete"] = False
    assert featured_keys(items, {"featured_papers": 1}) == {"other"}
    assert any(p.label == "方法机制" for p in paper_paragraphs(items[2], True))
    assert all(p.label != "方法机制" for p in paper_paragraphs(items[0]))


def test_rejected_result_cannot_leak_from_condition_metadata():
    item = paper()
    item.final_fields["key_result"] = "not_stated"
    item.final_fields["limitations"] = "not_stated"
    item.material.reading["claim_evidence"][0]["conditions"] = "rejected 99% improvement"
    text = " ".join(p.text for p in paper_paragraphs(item))
    assert "99%" not in text and "not_stated" not in text
    assert "本次未核验" in text and "不代表论文没有局限" in text


def test_cross_paper_comparison_requires_common_topic_and_preserves_tail():
    a, b = paper("a"), paper("b")
    a.final_fields["method"] = "routing 模型结构。" * 30 + "但不适用于有测量噪声的情况。"
    insights = build_daily_insights([a, b])
    assert any("但不适用于有测量噪声的情况" in i for i in insights)
    assert not any("值得追踪" in i for i in insights)
    b.material.tags = ["biology"]
    assert not any("方法对照" in i for i in build_daily_insights([a, b]))
    b.material.tags = ["routing"]
    b.material.reading["verification"]["valid_fields"] = []
    assert not any("方法对照" in i for i in build_daily_insights([a, b]))


def test_shared_metadata_tag_without_content_support_is_not_an_insight():
    a, b = paper('a'), paper('b')
    a.material.tags = b.material.tags = ['rag']
    a.final_fields['method'] = 'RAG 检索增强生成。'
    b.final_fields['method'] = '按 quantum fragment 路由逻辑补丁。'
    text = ' '.join(build_daily_insights([a, b]))
    assert '今日共同主题' not in text
    assert '方法对照' not in text


def test_card_does_not_repeat_rejected_claims_but_note_retains_audit():
    from daily_agent.rendering.notes import card_gaps, gaps
    item = paper()
    item.material.reading['verification']['issues'] = ['key_result: unsupported claim of 999 times acceleration']
    assert '999' not in card_gaps(item.material)
    assert '999' in gaps(item.material)
    assert '结果/发现部分内容未通过证据审核' in card_gaps(item.material)


def test_late_result_condition_is_not_cut_for_brevity():
    item = paper()
    item.material.reading['claim_evidence'] += [
        {'field':'key_result', 'evidence_kind':'simulation', 'conditions':'固定采样预算'},
        {'field':'key_result', 'evidence_kind':'simulation', 'conditions':'仅限无测量噪声，不能外推到真机'},
    ]
    for report in [render_daily_markdown([item], date(2026,9,28), RunStatus()),
                   render_daily_html([item], date(2026,9,28), RunStatus())]:
        assert '仅限无测量噪声，不能外推到真机' in report


def test_conditions_removed_only_when_independently_reviewed_and_current():
    from daily_agent.rendering.composition import review_result_presentation
    item=paper()
    review_result_presentation(item.final_fields['key_result'],item.material,
        lambda *_: {'supported':True,'conditions_complete':True,'issues':[]})
    result=paper_paragraphs(item)[1].text
    assert '结果性质/条件' not in result
    item.material.reading['claim_evidence'][0]['conditions'] += '；不可外推到硬件'
    assert '不可外推到硬件' in paper_paragraphs(item)[1].text


def test_failed_condition_coverage_preserves_all_conditions():
    from daily_agent.rendering.composition import review_result_presentation
    item=paper()
    review_result_presentation(item.final_fields['key_result'],item.material,
        lambda *_: {'supported':True,'conditions_complete':False,'issues':['missing cost']})
    assert '不计训练成本' in paper_paragraphs(item)[1].text


def test_fidelity_failures_distinguish_budget_timeout_and_rejection():
    from daily_agent.rendering.notes import card_gaps, gaps
    item = paper()
    item.material.reading['visual'] = {'required_pages': 4, 'fidelity': {'pages': [
        {'page': 1, 'passed': True},
        {'page': 2, 'passed': False, 'reason': 'FidelityBudgetExhausted'},
        {'page': 3, 'passed': False, 'reason': 'TimeoutExpired'},
        {'page': 4, 'passed': False, 'reason': '逐页重建/独立复核未通过'},
    ]}}
    for render in (card_gaps, gaps):
        text = render(item.material)
        assert '1 页核验预算耗尽' in text
        assert '1 页模型调用超时' in text
        assert '1 页转写或独立复核不通过' in text
