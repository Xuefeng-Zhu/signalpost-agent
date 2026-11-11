"""Scoped summaries and honest LLM accounting. All provider responses are offline mocks."""
import copy
import json
from unittest.mock import patch

import pytest

from test_synthesis import profile, T
from norway_company_agent.synthesis import build_synthesis, validate_synthesis, polish_with_llm, synthesis_cost, _chat
from norway_company_agent.batch import terminal_envelope
from run_competition_batch import attach_refresh_and_synthesis


def texts(s):
    return ' '.join(row['text'] for row in s['statements'])


def test_partial_nav_count_and_newest_are_observation_scoped():
    p = profile()
    p['evidence']['nav_jobs'].update(status='available', value={
        'collection_complete': False, 'candidates_total': 30, 'candidates_checked': 25,
        'postings': [{'uuid': str(i), 'title': f'Job {i}', 'published_at': '2026-01-01',
                      'evidence': copy.deepcopy(p['evidence']['site_signals'])} for i in range(25)]})
    s = build_synthesis(p)
    assert s['validation']['passed'], s['validation']
    assert 'at least 25 active ads' in texts(s)
    assert 'newest by publication date among the checked dated ads' in texts(s)
    assert 'additional active ads may exist' in texts(s)
    assert any(x['info_type'] == 'job_postings_coverage' for x in s['unknowns'])


def test_current_live_identity_and_flags_replace_stale_bulk():
    p = profile()
    p['evidence']['registry_live']['value'].update(organisation_number=p['organisation_number'],
        name='CURRENT ENTITY ASA', legal_form='ASA', bankrupt=True, liquidating=True)
    s = build_synthesis(p)
    assert 'CURRENT ENTITY ASA' in s['headline']
    assert 'public limited company' in s['headline']
    assert 'live register marks the entity as bankrupt' in texts(s)
    assert 'live register marks the entity as under liquidation' in texts(s)
    assert validate_synthesis(s, p)['passed']
    assert any('/registry_live/value/name' in f['pointer'] for st in s['statements'] for f in st['facts'])


def test_mismatched_live_identity_is_not_used_and_bulk_is_labelled():
    p = profile()
    p['evidence']['registry_live']['value'].update(organisation_number='999999999', name='OTHER ENTITY', bankrupt=True)
    s = build_synthesis(p)
    assert 'OTHER ENTITY' not in texts(s)
    assert 'historical fallbacks' in s['headline']
    assert 'live register marks' not in texts(s)


def test_modification_date_never_becomes_latest_publication():
    p = profile()
    v = p['evidence']['site_signals']['value']
    v['news_posts'].append({'url': 'https://eksempel.no/new', 'title': 'Modified only',
        'published_at': None, 'modified_at': '2026-11-11', 'evidence': copy.deepcopy(v['news_posts'][0]['evidence'])})
    v['news_posts_collection'] = {'collection_complete': False, 'window_limit': 10}
    s = build_synthesis(p)
    assert 'latest by publication date among these observed posts, dated 2024-08-09' in texts(s)
    assert '2026-11-11' not in texts(s)
    assert any(x['info_type'] == 'news_posts_coverage' for x in s['unknowns'])
    assert validate_synthesis(s, p)['passed']


def test_collection_window_change_and_bankruptcy_are_human_readable():
    p = profile()
    changes = [dict(field='site_signals.news_post_urls', change_type='collection_window_changed', material=False,
                    old_value=['a'], new_value=['b'], source_module='site_signals'),
               dict(field='registry.bankrupt', old_value=False, new_value=True, source_module='registry_live')]
    p['changes'] = changes
    s = build_synthesis(p, changes=changes, refresh={'previous_snapshot_supplied': True, 'company_in_previous_snapshot': True})
    assert 'missing items are not confirmed removals' in texts(s)
    assert 'Bankruptcy status: not marked as bankrupt → bankrupt' in texts(s)
    assert validate_synthesis(s, p)['passed']


def test_chat_retains_provider_usage():
    payload = {'choices': [{'message': {'content': 'test'}}], 'usage': {'prompt_tokens': 123, 'completion_tokens': 9}, 'model': 'mock', 'id': 'mock-id'}
    with patch('norway_company_agent.synthesis.urllib.request.urlopen') as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        response = _chat({'model': 'mock', 'api_key': 'mock', 'base_url': 'https://provider.invalid', 'timeout': '1'}, [])
    assert response['usage'] == payload['usage']
    assert response['response_id'] == 'mock-id'


@pytest.mark.parametrize('accepted', [True, False])
@pytest.mark.parametrize('charge', [None, 0.025])
def test_paid_response_accounting_survives_rejection_and_envelope(accepted, charge):
    p = profile()
    s = build_synthesis(p)
    st = s['statements'][0]
    content = st['text'] + ' ' + ''.join('[' + c + ']' for c in st['citations']) if accepted else 'Invented claim [S1]'
    usage = {'prompt_tokens': 100, 'completion_tokens': 20}
    if charge is not None:
        usage['cost_usd'] = charge
    p['synthesis'] = polish_with_llm(s, {'model': 'mock'}, chat=lambda *_: {'content': content, 'usage': usage})
    accounting = synthesis_cost(s)
    assert accounting['cost_usd'] == charge
    assert accounting['usage'] == usage
    env = terminal_envelope(p, run_id='test', modules=['registry'], started_at=T, completed_at=T)
    assert env['operations']['third_party_cost_usd'] == charge
    assert env['operations']['llm_accounting']['requests'] == 1


def test_failed_attempt_cost_is_unknown():
    def fail(*_):
        raise TimeoutError('offline timeout')
    s = polish_with_llm(build_synthesis(profile()), {'model': 'mock'}, chat=fail)
    assert s['llm_polish']['status'] == 'failed'
    assert synthesis_cost(s)['cost_usd'] is None


def test_batch_default_ignores_ambient_key_and_cost_is_zero(monkeypatch):
    monkeypatch.setenv('SIGNALPOST_LLM_API_KEY', 'mock-never-use')
    with patch('run_competition_batch.polish_with_llm', side_effect=AssertionError('must not call')):
        result = attach_refresh_and_synthesis([profile()], None)
    assert result['synthesis']['llm_polish']['requests'] == 0
    assert result['synthesis']['llm_polish']['cost_usd'] == 0


def test_batch_mixed_known_unknown_cost_is_not_zero(monkeypatch):
    monkeypatch.setenv('SIGNALPOST_LLM_API_KEY', 'mock-never-use')
    def mock_polish(s, config):
        s['llm_polish'] = {'status': 'rejected_by_post_check', 'accounting': {'requests': 1, 'cost_usd': None}}
    with patch('run_competition_batch.polish_with_llm', side_effect=mock_polish):
        result = attach_refresh_and_synthesis([profile()], None, use_llm=True)
    assert result['synthesis']['llm_polish']['cost_usd'] is None
    assert result['synthesis']['llm_polish']['unknown_cost_requests'] == 1


@pytest.mark.parametrize('opt_in', [False, True])
def test_cli_requires_explicit_opt_in_and_reports_mock_charge(tmp_path, monkeypatch, opt_in):
    import gzip
    import sys
    from pathlib import Path
    import run_competition_batch as batch
    root = Path(__file__).resolve().parents[1]
    bulk = tmp_path / 'bulk.gz'
    bulk.write_bytes(gzip.compress((root / 'tests/fixtures/bulk_quoted_row.csv').read_bytes()))
    orgs = tmp_path / 'orgs.txt'
    orgs.write_text('818711832\n')
    monkeypatch.setenv('SIGNALPOST_LLM_API_KEY', 'mock-never-use')
    def official(org, modules):
        assert not modules
        return {}, []
    monkeypatch.setattr(batch, 'fetch_official_modules', official)
    calls = []
    def mock_polish(s, config):
        calls.append(True)
        s['llm_polish'] = {'status': 'rejected_by_post_check', 'accounting': {
            'requests': 1, 'cost_usd': 0.025, 'cost_status': 'provider_reported',
            'cost_provenance': 'offline fixture usage.cost_usd', 'usage': {'cost_usd': 0.025}}}
    monkeypatch.setattr(batch, 'polish_with_llm', mock_polish)
    monkeypatch.setattr(sys, 'argv', ['batch', '--organisations', str(orgs), '--bulk', str(bulk),
        '--output', str(tmp_path / 'envelopes.jsonl'), '--profiles-output', str(tmp_path / 'profiles.jsonl'),
        '--report', str(tmp_path / 'report.json'), '--run-id', 'offline', '--expected-count', '1',
        '--modules', 'registry,accounting_obligation', *(['--llm-synthesis'] if opt_in else [])])
    with pytest.raises(SystemExit) as result:
        batch.main()
    assert result.value.code == 0
    assert bool(calls) is opt_in
    report = json.loads((tmp_path / 'report.json').read_text())
    envelope = json.loads((tmp_path / 'envelopes.jsonl').read_text())
    assert report['operations']['third_party_cost_usd'] == (0.025 if opt_in else 0)
    assert envelope['operations']['third_party_cost_usd'] == (0.025 if opt_in else 0)


@pytest.mark.parametrize('live_org,live_status,use_live', [
    ('818711832', 'available', True), ('999999999', 'available', False),
    ('818711832', 'source_error', False), (None, 'available', False),
])
def test_enrich_passes_current_verified_identity_to_gate(tmp_path, monkeypatch, live_org, live_status, use_live):
    import gzip
    import sys
    from pathlib import Path
    import run_competition_batch as batch
    from norway_company_agent.evidence import evidence
    root = Path(__file__).resolve().parents[1]
    bulk = tmp_path / 'bulk.gz'
    bulk.write_bytes(gzip.compress((root / 'tests/fixtures/bulk_quoted_row.csv').read_bytes()))
    orgs = tmp_path / 'orgs.txt'
    orgs.write_text('818711832\n')
    def official(org, modules):
        assert modules == {'registry_live'}
        return {'registry_live': evidence('registry_live', live_status, 'official_registry_api',
            f'https://data.brreg.no/enhetsregisteret/api/enheter/{org}',
            value={'organisation_number': live_org, 'name': 'CURRENT NAME ASA', 'legal_form': 'ASA'})}, []
    monkeypatch.setattr(batch, 'fetch_official_modules', official)
    seen = []
    def gate(p, **kwargs):
        seen.append(copy.deepcopy(p))
        return evidence('website', 'not_found', 'registry_linked_company_website', 'https://example.no/'), {
            'requests': 0, 'bytes': 0, 'latencies_ms': [], 'dns_lookups': 0}
    monkeypatch.setattr(batch, 'resolve_official_website', gate)
    monkeypatch.setattr(sys, 'argv', ['batch', '--organisations', str(orgs), '--bulk', str(bulk),
        '--output', str(tmp_path / 'envelopes.jsonl'), '--profiles-output', str(tmp_path / 'profiles.jsonl'),
        '--report', str(tmp_path / 'report.json'), '--run-id', 'offline', '--expected-count', '1',
        '--modules', 'registry,registry_live,website', '--no-llm-synthesis'])
    with pytest.raises(SystemExit) as result:
        batch.main()
    assert result.value.code == 0
    assert len(seen) == 1
    original = seen[0]['evidence']['registry']['value']
    assert original['navn'] == 'LUFTAMBULANSETJENESTEN HF'
    expected_name = 'CURRENT NAME ASA' if use_live else original['navn']
    expected_form = 'ASA' if use_live else original['organisasjonsform.kode']
    assert seen[0]['name'] == expected_name
    assert seen[0]['legal_form'] == expected_form
    provenance = seen[0]['registry_identity_fields']['name']
    assert provenance['module'] == ('registry_live' if use_live else 'registry')
    assert provenance['fallback'] is (not use_live)
    if not use_live:
        assert 'Bulk registry snapshot' in provenance['note']
    envelope = json.loads((tmp_path / 'envelopes.jsonl').read_text())
    assert envelope['profile']['name'] == expected_name
    assert expected_name in envelope['synthesis']['headline']
    assert envelope['profile']['evidence']['registry']['value'] == original
