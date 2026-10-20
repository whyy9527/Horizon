import asyncio
import hashlib
import json
from argparse import Namespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import trafilatura

from src.extractors.html_components import clean_registration_cards
from src.extractors.trafilatura import TrafilaturaExtractor
from src.extractors import ExtractorRegistry
from src.models import RSSSourceConfig, TrafilaturaExtractorConfig
from src.scrapers.rss import RSSScraper


STORY = """<div class="article-content">
<h1>A city restores its old library</h1>
<p>The city council restored the old library after several years of planning.
Residents can now borrow books, study together, and use the renovated reading
rooms. The mayor said the project was funded by the city and local donations.</p>
<p>Researchers compared the opening hours with the previous year and found
that residents had more time to visit. The survey included weekday and weekend
visits, and the complete results are recorded below for public inspection.</p>
<table><tr><th>Year</th><th>Hours</th></tr><tr><td>2026</td><td>72</td></tr></table>
<p>The library will publish its next report at the end of the year. Visitors
will be able to comment on the results and suggest changes to the service.</p>
</div>"""


def card(target="/signup", description="Discover our collection with a free account."):
    return f'<div><h3>Join the collection</h3><p>{description}</p><a href="{target}">Join</a></div>'


def page(before="", body=STORY, after=""):
    return f"<html><body>{before}{body}{after}</body></html>"


@pytest.mark.parametrize("target", ["/signup", "/sign-up/", "https://service.example/register?from=news", "/registration"])
def test_registration_card_removed_without_publisher_or_copy_matching(target):
    source = page(card(target))
    cleaned, receipt = clean_registration_cards(source)
    assert 'Join the collection' not in cleaned
    assert '2026' in cleaned and '<table>' in cleaned
    assert receipt['input_sha256'] == hashlib.sha256(source.encode()).hexdigest()
    assert receipt['output_sha256'] == hashlib.sha256(cleaned.encode()).hexdigest()
    assert receipt['removals'][0]['target'] == target
    assert 'Discover our collection' in receipt['removals'][0]['serialized_html']
    assert 'data-horizon-cleaning-node' not in cleaned
    assert clean_registration_cards(cleaned) == (cleaned, {})


@pytest.mark.parametrize("source", [
    page('<p>The report describes a <a href="/signup">registration page</a> used by the service.</p>'),
    page(body='<article>'+STORY+card()+'</article>'),
    page(body='<div itemprop="articleBody">'+STORY+card()+'</div>'),
    page(card('/pricing')),
    page(card('javascript:/signup')),
    page('<div><h3>Example</h3><p><code>register()</code></p><a href="/register">Read</a></div>'),
    page('<div><h3>Example</h3><p>Quoted content</p><blockquote>Keep this</blockquote><a href="/register">Read</a></div>'),
])
def test_editorial_links_article_cards_code_and_quotes_remain_byte_for_byte(source):
    assert clean_registration_cards(source) == (source, {})


def test_card_selected_as_main_content_is_preserved():
    source = page(body=card(description=' '.join(['The service explains how its registration works.']*20)))
    assert clean_registration_cards(source) == (source, {})


def test_multiple_cards_record_paths_before_any_node_is_removed():
    source = page(card()+card('/register'))
    _, receipt = clean_registration_cards(source)
    assert [removal['xpath'] for removal in receipt['removals']] == [
        '/html/body/div[1]', '/html/body/div[2]',
    ]


def test_extraction_keeps_tables_and_has_no_implicit_precision_switch():
    extractor = TrafilaturaExtractor(TrafilaturaExtractorConfig())
    text, receipt = extractor.extract_text_with_receipt(page(card()))
    assert text == trafilatura.extract(page())
    assert '72' in text
    assert len(receipt['removals']) == 1


def test_component_cleaning_failure_keeps_original_extraction():
    extractor = TrafilaturaExtractor(TrafilaturaExtractorConfig())
    source = page()
    with patch('src.extractors.trafilatura.clean_registration_cards', side_effect=ValueError('invalid candidate')):
        text, receipt = extractor.extract_text_with_receipt(source)
    assert text == trafilatura.extract(source)
    assert receipt == {'status': 'failed', 'error': 'ValueError'}


def test_concurrent_rss_requests_keep_their_own_cleaning_receipts():
    source = RSSSourceConfig(name='Example', url='https://example.com/feed',
                             content_extractor='trafilatura', require_content_extraction=True)
    scraper = RSSScraper([source], AsyncMock(), ExtractorRegistry({}))

    async def response(_client, _method, url, **_kwargs):
        result = MagicMock()
        result.headers = {}
        result.text = page(card()) if url.endswith('/one') else page()
        return result

    async def run():
        return await asyncio.gather(*[
            scraper._build_item(source, {'title': 'Library', 'link': f'https://example.com/{slug}'},
                                datetime.now(timezone.utc)) for slug in ('one', 'two')
        ])

    with patch('src.extractors.trafilatura.safe_request', side_effect=response):
        first, second = asyncio.run(run())
    assert first.content == second.content
    assert first.metadata['content_extraction']['html_cleaning']['removals']
    assert 'html_cleaning' not in second.metadata['content_extraction']


def test_url_cli_preserves_cleaning_receipt(tmp_path):
    from src.url_extract_cli import run

    manifest = tmp_path/'urls.json'
    manifest.write_text(json.dumps({'urls': [{'id': 'news', 'url': 'https://example.com/news'}]}))
    response = MagicMock()
    response.headers = {}
    response.text = page(card())
    with patch('src.extractors.trafilatura.safe_request', new=AsyncMock(return_value=response)):
        result = asyncio.run(run(Namespace(input=str(manifest), extractor='trafilatura',
                                          timeout=8, article_concurrency=2)))
    assert result['receipts'][0]['html_cleaning']['removals'][0]['target'] == '/signup'
    assert 'Join the collection' not in result['items'][0]['content']
