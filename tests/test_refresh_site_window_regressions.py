"""Offline regressions for unordered evidence, windowed news and date semantics."""
import copy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import tldextract

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from norway_company_agent import site_signals as ss
from norway_company_agent.refresh import diff_profile
from norway_company_agent.website import normalize_social_url
from test_synthesis import profile


@pytest.fixture(autouse=True)
def offline_suffixes(monkeypatch):
    monkeypatch.setattr(tldextract, 'extract', tldextract.TLDExtract(suffix_list_urls=()))


@pytest.mark.parametrize('module,key', [('roles', 'roles'), ('financials', 'records'), ('financial_history', 'years'), ('locations', 'locations')])
def test_order_is_not_revision_but_real_content_is(module, key):
    old = profile()
    old['evidence'][module] = {'status': 'available', 'value': {key: [
        {'id': 'a', 'amount': 1, 'items': [{'name': 'x'}, {'name': 'y'}]},
        {'id': 'b', 'amount': 2},
    ]}}
    new = copy.deepcopy(old)
    new['evidence'][module]['value'][key].reverse()
    assert diff_profile(old, new) == []
    new['evidence'][module]['value'][key][1]['amount'] = 9
    changes = diff_profile(old, new)
    assert len(changes) == 1 and changes[0]['material']


@pytest.mark.parametrize('key', ['bankrupt', 'liquidating', 'forced_dissolution'])
def test_live_registry_adverse_status_is_tracked(key):
    old = profile()
    old['evidence']['registry_live']['value']['organisation_number'] = old['organisation_number']
    old['evidence']['registry_live']['value'][key] = False
    new = copy.deepcopy(old)
    new['evidence']['registry_live']['value'][key] = True
    change, = diff_profile(old, new)
    assert change['field'] == f'registry.{key}'
    assert change['source_module'] == 'registry_live'
    assert change['material'] is True


def feed(n):
    return ('<rss><channel>' + ''.join(
        f'<item><title>News item {i}</title><link>https://acme.no/news/{i}</link><pubDate>{i:02d} Nov 2026 10:00:00 +0000</pubDate></item>'
        for i in range(1, n + 1)) + '</channel></rss>').encode()


def collect(n):
    def fetch(url, budget, **kwargs):
        budget.take()
        raw = b'' if url.endswith('robots.txt') else feed(n) if url.endswith('feed') else b'<html><title>ACME</title><body>916340257<link rel="alternate" type="application/rss+xml" href="/feed"></body></html>'
        return {'ok': True, 'status': 200, 'raw': raw, 'final_url': url, 'retrieved_at': 't', 'content_sha256': 'h'}
    claim = {'value': 'https://acme.no/', 'identity_method': 'organisation_number', 'evidence': {'source_url': 'https://acme.no/'}}
    with patch.object(ss, '_fetch', fetch):
        return ss.collect_site_signals({'organisation_number': '916340257', 'name': 'ACME AS'}, {'official_website': claim, 'value': {'final_url': 'https://acme.no/'}})[0]


def test_feed_ten_to_eleven_is_a_window_shift_not_deletion():
    old = profile()
    old['evidence']['site_signals'] = collect(10)
    new = copy.deepcopy(old)
    new['evidence']['site_signals'] = collect(11)
    meta = new['evidence']['site_signals']['value']['news_posts_collection']
    assert meta == {'collection_complete': False, 'window_complete': False, 'window_limit': 10,
                    'observed_count': 11, 'returned_count': 10, 'truncated': True}
    change, = diff_profile(old, new)
    assert change['material'] is False
    assert change['change_type'] == 'collection_window_changed'
    assert change['observed_missing_ids'] == ['https://acme.no/news/1']
    assert change['observed_added_ids'] == ['https://acme.no/news/11']


def test_legacy_news_without_completeness_cannot_prove_deletion():
    old = profile()
    old['evidence']['site_signals'] = {'status': 'available', 'value': {'news_posts': [{'url': 'https://acme.no/news/1'}]}}
    new = copy.deepcopy(old)
    new['evidence']['site_signals']['value']['news_posts'] = []
    assert diff_profile(old, new)[0]['material'] is False


def test_explicit_complete_news_collections_can_confirm_change():
    old = profile()
    old['evidence']['site_signals'] = {'status': 'available', 'value': {'news_posts': [{'url': 'https://acme.no/news/1'}], 'news_posts_collection': {'collection_complete': True}}}
    new = copy.deepcopy(old)
    new['evidence']['site_signals']['value']['news_posts'] = []
    assert diff_profile(old, new)[0]['material'] is True


def test_sitemap_lastmod_is_never_publication():
    result = {'raw': b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://acme.no/news/old</loc><lastmod>2026-11-01</lastmod></url></urlset>',
              'final_url': 'https://acme.no/sitemap.xml', 'retrieved_at': 't', 'content_sha256': 'h'}
    post, = ss._sitemap_news(result, 'acme.no', ss._Budget(1, 1), lambda *args: pytest.fail('Unexpected fetch'))
    assert post['published_at'] is None and post['date_source'] is None
    assert post['modified_at'] == '2026-11-01'
    assert post['modified_date_source'] == 'sitemap_lastmod'


def test_atom_updated_is_not_published():
    post, = ss.parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Old news</title><link href="https://acme.no/news/old"/><updated>2026-11-01</updated></entry></feed>', 'acme.no')
    assert post['published_at'] is None
    assert post['modified_at'] == '2026-11-01'


@pytest.mark.parametrize('url', ['https://facebook.com/acme/posts/123', 'https://tiktok.com/@acme/video/123', 'https://instagram.com/acme/reel/123'])
def test_social_content_urls_are_not_profiles(url):
    assert normalize_social_url(url) is None


@pytest.mark.parametrize('url', ['https://facebook.com/acme', 'https://tiktok.com/@acme', 'https://facebook.com/p/Acme-123456', 'https://facebook.com/people/Acme/123456'])
def test_social_profiles_still_work(url):
    assert normalize_social_url(url)['url'] == url


def test_nested_address_line_order_is_not_silently_erased():
    old = profile()
    old['evidence']['locations'] = {'status': 'available', 'value': {'locations': [
        {'organisation_number': '123', 'address': {'adresse': ['Line one', 'Line two']}}
    ]}}
    new = copy.deepcopy(old)
    new['evidence']['locations']['value']['locations'][0]['address']['adresse'].reverse()
    assert diff_profile(old, new)[0]['material'] is True


def test_modified_time_element_is_not_publication():
    page = ss._parse_page({'raw': b'<article><a href="/news/old-article">An old article with a new edit</a><time itemprop="dateModified" datetime="2026-11-01">1 Nov 2026</time></article>',
                          'final_url': 'https://acme.no/news', 'retrieved_at': 't', 'content_sha256': 'h'})
    post, = ss.extract_index_posts(page, 'https://acme.no/news', 'acme.no')
    assert post['published_at'] is None
    assert post['modified_at'] == '2026-11-01'


def test_wrong_entity_live_record_cannot_confirm_registry_changes():
    old = profile()
    old['evidence']['registry_live']['value'].update(organisation_number=old['organisation_number'], employees=71)
    new = copy.deepcopy(old)
    new['evidence']['registry_live']['value'].update(organisation_number='999999999', employees=99)
    changes = diff_profile(old, new)
    assert changes and all(not c['material'] for c in changes)


def test_boolean_string_equivalence_and_unknown_flags():
    old = profile()
    old['evidence']['registry_live']['value'].update(organisation_number=old['organisation_number'], bankrupt=False)
    new = copy.deepcopy(old)
    new['evidence']['registry_live']['value']['bankrupt'] = 'false'
    assert diff_profile(old, new) == []
    new['evidence']['registry_live']['value']['bankrupt'] = 'not-a-boolean'
    assert diff_profile(old, new)[0]['material'] is False


@pytest.mark.parametrize('path', [
    'photo.php?fbid=123456', 'story.php?story_fbid=123456&id=999', 'watch/?v=123456',
    'permalink.php?story_fbid=123456&id=999', 'video.php?v=123456',
    'photo/?fbid=123456', 'photos', 'videos', 'reel/123456', 'reels',
    'stories/999/123456', 'share/p/123456', 'l.php?u=https://elsewhere.no',
])
def test_facebook_reserved_content_is_rejected_even_in_same_as(path):
    import json
    url = 'https://facebook.com/' + path
    assert normalize_social_url(url) is None
    html = ('<a href="' + url + '">Facebook</a><script type="application/ld+json">' +
            json.dumps({'@type': 'Organization', 'name': 'ACME AS', 'sameAs': [url]}) + '</script>')
    page = ss._parse_page({'raw': html.encode(), 'final_url': 'https://acme.no/',
                          'retrieved_at': 't', 'content_sha256': 'h'})
    published, candidates = ss.extract_social([page], ss.brand_tokens({'name': 'ACME AS'}, 'https://acme.no/'))
    assert published == []
    assert candidates == []
