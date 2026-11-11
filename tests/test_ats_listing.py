import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from norway_company_agent import site_signals as ss  # noqa: E402


def page(url, html):
    return ss._parse_page({'raw': html.encode(), 'final_url': url, 'retrieved_at': 't', 'content_sha256': 'h'})


def test_teamtailor_root_maps_to_jobs_list():
    assert ss.ats_listing_url('https://acme.teamtailor.com/') == 'https://acme.teamtailor.com/jobs'
    assert ss.ats_listing_url('https://acme.teamtailor.com/jobs/123-x') == 'https://acme.teamtailor.com/jobs/123-x'
    assert ss.ats_listing_url('https://acme.recman.no/index.php') == 'https://acme.recman.no/index.php'


def test_ats_page_postings_drop_open_application():
    p = page('https://acme.teamtailor.com/jobs', '<a href="/jobs/1-open-application">Open application</a>'
             '<a href="/jobs/2-senior-developer">Senior developer</a><a href="/jobs/3-apen-soknad">Åpen søknad til oss</a>')
    titles = [x['title'] for x in ss.postings_from_ats_page(p)]
    assert titles == ['Senior developer']


def test_site_linking_teamtailor_gets_its_listed_jobs():
    home = ('<html><title>ACME</title><body>ACME AS org.nr 916340257'
            '<a href="https://acme.teamtailor.com/">Jobb hos oss</a></body></html>')
    jobs = ('<html><body><a href="https://acme.teamtailor.com/jobs/77-lagerarbeider">Lagerarbeider Oslo</a>'
            '<a href="https://acme.teamtailor.com/jobs/78-sales-rep">Sales rep Stockholm</a></body></html>')

    def ad(country):
        return ('<html><script type="application/ld+json">{"@type": "JobPosting", "title": "x", "datePosted": "2026-11-01",'
                '"jobLocation": {"@type": "Place", "address": {"addressCountry": "%s"}}}</script></html>' % country)

    def fetch(url, budget, **kwargs):
        budget.take()
        raw = (b'' if url.endswith('robots.txt') else ad('NO').encode() if '/jobs/77' in url else ad('SE').encode() if '/jobs/78' in url
               else jobs.encode() if 'teamtailor.com/jobs' in url else home.encode())
        status = 404 if url.endswith('robots.txt') else 200
        return {'ok': status == 200, 'status': status, 'raw': raw, 'final_url': url, 'retrieved_at': 't', 'content_sha256': 'h'}

    claim = {'value': 'https://acme.no/', 'identity_method': 'organisation_number', 'evidence': {'source_url': 'https://acme.no/'}}
    with patch.object(ss, '_fetch', fetch):
        record = ss.collect_site_signals({'organisation_number': '916340257', 'name': 'ACME AS'},
                                         {'official_website': claim, 'value': {'final_url': 'https://acme.no/'}})[0]
    postings = record['value']['careers']['postings']
    assert [p['title'] for p in postings] == ['Lagerarbeider Oslo']
    assert postings[0]['method'] == 'ats_listing_linked_from_verified_site'
    assert postings[0]['evidence']['source_url'] == 'https://acme.teamtailor.com/jobs/77-lagerarbeider'
    assert postings[0]['date_posted'].startswith('2026-11-01')


def test_ats_ad_expired_or_foreign_is_dropped():
    def ad(body):
        return page('https://a.teamtailor.com/jobs/1', '<script type="application/ld+json">%s</script>' % body)
    no = '{"@type":"JobPosting","jobLocation":[{"address":{"addressCountry":"NO"}}]%s}'
    assert ss.ats_ad_in_norway(ad(no % ''), today='2026-11-11')
    assert ss.ats_ad_in_norway(ad(no % ',"validThrough":"2026-10-01"'), today='2026-11-11') is None
    assert ss.ats_ad_in_norway(ad('{"@type":"JobPosting","jobLocation":{"address":{"addressCountry":"GB"}}}'), today='2026-11-11') is None
    assert ss.ats_ad_in_norway(ad('{"@type":"JobPosting"}'), today='2026-11-11') is None
