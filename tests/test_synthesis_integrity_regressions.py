"""Adversarial factual-integrity tests; no network or LLM service is called."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.synthesis import (
    build_synthesis, check_narrative, polish_with_llm, resolve_pointer, validate_synthesis,
)
from test_synthesis import profile


def render(statement):
    return statement['text'] + ' ' + ''.join(f'[{c}]' for c in statement['citations'])


@pytest.mark.parametrize('text', [
    'The company is bankrupt and has dismissed 71 employees. [S1]',
    'The register does not report 71 employees. [S1]',
    'The company doubled its revenue. [S1]',
    'The company is profitable. [S1]',
    'The company sells office furniture. [S1]',
])
def test_shared_names_numbers_or_citations_cannot_authorize_invented_predicates(text):
    s = {'statements': [{'text': 'The register reports 71 employees.', 'citations': ['S1']}]}
    result = polish_with_llm(s, {'model': 'offline-fixture'}, chat=lambda *_: text)
    assert result['llm_polish']['status'] == 'rejected_by_post_check'
    assert 'narrative' not in result['llm_polish']


def test_llm_can_select_reorder_and_reflow_whole_statements_only():
    synthesis = build_synthesis(profile())
    selected = [synthesis['statements'][4], synthesis['statements'][0]]
    text = '\n\n'.join(render(s) for s in selected)
    result = polish_with_llm(synthesis, {'model': 'offline-fixture'}, chat=lambda *_: text)
    assert result['llm_polish']['status'] == 'accepted'
    assert validate_synthesis(result, profile())['passed']


def test_multi_sentence_statement_must_be_kept_whole_with_all_citations():
    s = {'statements': [{'text': 'Revenue is unknown. Debt is unknown.', 'citations': ['S1', 'S2']}]}
    assert check_narrative('Revenue is unknown. [S1][S2]', s)
    assert check_narrative('Revenue is unknown. Debt is unknown. [S1]', s)
    assert not check_narrative('Revenue is unknown. Debt is unknown. [S1][S2]', s)


@pytest.mark.parametrize('field,value', [
    ('content_sha256', 'f' * 64),
    ('retrieved_at', '1900-01-01T00:00:00Z'),
    ('evidence_pointer', '/evidence/not-a-record'),
    ('source_url', 'https://unrelated.test/'),
    ('source_class', 'invented_source'),
    ('claim_span', 'unsupported claim'),
])
def test_source_metadata_is_bound_to_exact_evidence_record(field, value):
    p = profile()
    s = build_synthesis(p)
    s['sources'][0][field] = value
    assert not validate_synthesis(s, p)['passed']


def test_employee_fact_cannot_cite_unrelated_real_website_source():
    p = profile()
    s = build_synthesis(p)
    size = next(x for x in s['statements'] if x['section'] == 'size')
    website_source = next(x for x in s['sources'] if x['source_url'] == 'https://eksempel.no/')
    size['citations'] = [website_source['id']]
    assert not validate_synthesis(s, p)['passed']


def test_predicate_change_with_unchanged_facts_is_rejected():
    p = profile()
    s = build_synthesis(p)
    size = next(x for x in s['statements'] if x['section'] == 'size')
    size['text'] = size['text'].replace('reports', 'dismissed')
    assert not validate_synthesis(s, p)['passed']


def test_projection_cannot_invent_strings_without_digits():
    p = profile()
    s = build_synthesis(p)
    target = next(x for x in s['statements'] if x['section'] == 'locations' and any(isinstance(f['value'], list) for f in x['facts']))
    projected = next(f for f in target['facts'] if isinstance(f['value'], list))
    projected['value'] = ['made up unrelated content']
    assert not validate_synthesis(s, p)['passed']


def test_previously_accepted_narrative_is_rechecked_during_envelope_validation():
    p = profile()
    s = build_synthesis(p)
    s['llm_polish'] = {'status': 'accepted', 'narrative': 'The company is bankrupt. [S1]'}
    assert not validate_synthesis(s, p)['passed']


@pytest.mark.parametrize('ptr', ['evidence/registry', '/rows/-1', '/rows/01'])
def test_noncanonical_json_pointer_does_not_resolve(ptr):
    with pytest.raises((KeyError, ValueError)):
        resolve_pointer({'evidence': {'registry': 1}, 'rows': [1, 2]}, ptr)


def test_source_deduplication_keeps_separate_record_provenance():
    p = profile()
    # Same URL/hash/label but separate contact facts need their own source pointers.
    contact = p['evidence']['site_signals']['value']['contact']
    first = contact['emails'][0]
    contact['emails'].append({'value': 'hello@eksempel.no', 'evidence': copy.deepcopy(first['evidence'])})
    s = build_synthesis(p)
    contact_sources = [x for x in s['sources'] if x['label'] == 'Verified company website (contact)']
    assert len(contact_sources) == 2
    assert len({x['evidence_pointer'] for x in contact_sources}) == 2
    assert validate_synthesis(s, p)['passed']


def test_dated_news_subset_does_not_drop_supported_statement():
    p = profile()
    news = p['evidence']['site_signals']['value']['news_posts']
    news.append({'title': 'Undated item', 'url': 'https://eksempel.no/undated',
                 'evidence': copy.deepcopy(news[0]['evidence'])})
    s = build_synthesis(p)
    assert s['validation']['passed'], s['validation']
    assert any('1 dated news/blog post' in st['text'] for st in s['statements'])
    assert validate_synthesis(s, p)['passed']
