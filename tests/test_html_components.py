import asyncio
import hashlib
import json
from pathlib import Path
from argparse import Namespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import trafilatura
from lxml import html as lxml_html

from src.extractors.html_components import clean_registration_cards, prepare_article_content
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


def test_inline_email_signup_removes_component_and_only_explicit_adjacent_promos():
    source = '''<html><body><article><h1>Report</h1><div class="body markup">
<p><strong>Thanks for reading! If you haven't subscribed, click the button below:</strong></p>
<div class="subscribe-widget" data-component-name="SubscribeWidget"><form action="/api/v1/free" method="post"><input type="email"/><button type="submit">Subscribe</button></form></div>
<p><strong>By subscribing, you'll join over 80,000 readers!</strong></p>
<p>The article examines how a subscription business model affects publisher revenue.</p>
</div></article></body></html>'''

    cleaned, receipt = clean_registration_cards(source)
    assert 'Thanks for reading' not in cleaned
    assert "By subscribing" not in cleaned
    assert 'type="email"' not in cleaned
    assert 'subscription business model affects publisher revenue' in cleaned
    assert [row['rule'] for row in receipt['removals']] == [
        'adjacent_subscription_promotion_paragraph',
        'adjacent_subscription_promotion_paragraph',
        'email_signup_subscription_component',
    ]
    assert [row['xpath'] for row in receipt['removals']] == [
        '/html/body/article/div/p[1]', '/html/body/article/div/p[2]',
        '/html/body/article/div/div',
    ]
    original_dom = lxml_html.document_fromstring(source)
    for row in receipt['removals']:
        original_node = original_dom.xpath(row['xpath'])[0]
        assert row['text'] == original_node.text_content()
        assert row['serialized_html'] == lxml_html.tostring(
            original_node, encoding='unicode', with_tail=False)
    assert receipt['input_sha256'] == hashlib.sha256(source.encode()).hexdigest()
    assert receipt['output_sha256'] == hashlib.sha256(cleaned.encode()).hexdigest()
    assert clean_registration_cards(cleaned) == (cleaned, {})


def test_multiple_inline_subscription_widgets_keep_original_paths_and_clear_promos():
    widget = ('<div class="subscription-widget-wrap"><div class="preamble">'
              '<p>Thanks for reading! To receive new posts, consider subscribing!</p></div>'
              '<div class="subscribe-widget" data-component-name="SubscribeWidget">'
              '<form><input type="email"/><button type="submit">Subscribe</button></form>'
              '</div></div>')
    source = '<article><h1>Report</h1>'+widget+'<p>Real paragraph between sections.</p>'+widget+'</article>'

    cleaned, receipt = clean_registration_cards(source)
    assert 'subscription-widget-wrap' not in cleaned
    assert 'Thanks for reading' not in cleaned
    assert 'Real paragraph between sections.' in cleaned
    assert [row['xpath'] for row in receipt['removals'] if row['rule'] == 'email_signup_subscription_component'] == [
        '/html/body/article/div[1]', '/html/body/article/div[2]',
    ]
    assert clean_registration_cards(cleaned) == (cleaned, {})


@pytest.mark.parametrize('container', ['blockquote', 'q', 'cite', 'pre', 'code', 'table'])
def test_subscription_like_forms_in_quoted_code_or_tables_are_preserved(container):
    widget = '<div class="subscribe-widget" data-component-name="SubscribeWidget"><form><input type="email"/><button type="submit">Subscribe</button></form></div>'
    source = f'<article><h1>Report</h1><{container}>{widget}</{container}><p>Article prose.</p></article>'
    assert clean_registration_cards(source) == (source, {})


def test_subscription_language_without_component_or_email_form_is_preserved():
    source = ('<article><h1>Report</h1><p>Thanks for reading! Please subscribe below.</p>'
              '<div class="subscribe-widget"><form><input type="text"/><button type="submit">Send</button></form></div>'
              '<p>By subscribing, you will join readers who support this publication.</p></article>')
    assert clean_registration_cards(source) == (source, {})


def test_only_directly_adjacent_cta_paragraphs_are_removed():
    widget = '<div class="subscribe-widget" data-component-name="SubscribeWidget"><form><input type="email"/><button type="submit">Subscribe</button></form></div>'
    source = ('<article><h1>Report</h1><p><strong>Thanks for reading! Subscribe below.</strong></p>'
              '<figure><img src="chart.png"/><figcaption>Subscription growth by year.</figcaption></figure>'
              +widget+
              '<p><strong>Subscriptions are the publication’s business model.</strong></p>'
              '<p>A citation links to <a href="https://example.com/subscribe">the subscription terms</a>.</p>'
              '</article>')
    cleaned, receipt = clean_registration_cards(source)
    assert 'Thanks for reading' in cleaned  # A figure separates this CTA-like text from the form.
    assert 'Subscription growth by year.' in cleaned
    assert 'Subscriptions are the publication’s business model.' in cleaned
    assert 'https://example.com/subscribe' in cleaned
    assert [row['rule'] for row in receipt['removals']] == ['email_signup_subscription_component']


def test_marked_wrapper_with_editorial_content_keeps_content_and_removes_only_inner_widget():
    source = ('''<article><h1>Report</h1><div class="subscription-widget-wrap">
<div class="preamble">Editorial note.<p>Thanks for reading! To receive more new posts, consider subscribing!.</p></div>
<p>The publication's subscription business model changed after the 2024 price increase.</p>
<h2>Subscription revenue changes</h2>
<figure><img src="chart.png"/><figcaption>Subscription revenue by year.</figcaption></figure>
<pre><code>signup.submit(email)</code></pre>
<div class="subscribe-widget" data-component-name="SubscribeWidget"><form><input type="email"/><button type="submit">Subscribe</button></form></div>
</div></article>''')

    cleaned, receipt = clean_registration_cards(source)
    assert 'subscription-widget-wrap' in cleaned
    assert 'Thanks for reading' not in cleaned
    assert 'Editorial note.' in cleaned
    assert "subscription business model changed" in cleaned
    assert 'Subscription revenue changes' in cleaned
    assert 'Subscription revenue by year.' in cleaned
    assert 'signup.submit(email)' in cleaned
    assert 'type="email"' not in cleaned
    assert [row['rule'] for row in receipt['removals']] == [
        'subscription_preamble_promotion_paragraph', 'email_signup_subscription_component',
    ]


def test_marked_component_with_its_own_editorial_content_is_preserved():
    widget = ('<div class="subscribe-widget" data-component-name="SubscribeWidget">'
              '<p>The article discusses subscription business models.</p>'
              '<blockquote><p>By subscribing, readers fund the newsletter.</p></blockquote>'
              '<form><input type="email"/><button type="submit">Subscribe</button></form></div>')
    source = '<article><h1>Report</h1>'+widget+'</article>'
    assert clean_registration_cards(source) == (source, {})


@pytest.mark.parametrize('container', ['blockquote', 'q', 'cite', 'pre', 'code', 'table', 'figure'])
def test_unsafe_wrapper_keeps_preamble_cta_inside_editorial_ancestor(container):
    widget = ('<div class="subscribe-widget" data-component-name="SubscribeWidget">'
              '<form><input type="email"/><button type="submit">Subscribe</button></form></div>')
    source = (f'<article><h1>Report</h1><div class="subscription-widget-wrap">'
              f'<{container}><div class="preamble"><p><strong>'
              'By subscribing, you will join over 80,000 people who read this newsletter!'
              f'</strong></p></div></{container}>{widget}</div></article>')
    cleaned, receipt = clean_registration_cards(source)
    assert 'By subscribing, you will join over 80,000 people who read this newsletter!' in cleaned
    assert 'type="email"' not in cleaned
    assert [row['rule'] for row in receipt['removals']] == ['email_signup_subscription_component']


@pytest.mark.parametrize('paragraph', [
    '<p><strong>By subscribing, you will join over 80,000 readers!</strong> The article questions the data.</p>',
    '<p><strong>The author wrote “Thanks for reading! If you haven’t subscribed, click the button below.”</strong></p>',
    '<p><strong>By subscribing, readers can compare several competing business models.</strong></p>',
])
def test_cta_words_inside_editorial_paragraph_do_not_remove_mixed_or_quoted_text(paragraph):
    widget = '<div class="subscribe-widget" data-component-name="SubscribeWidget"><form><input type="email"/><button type="submit">Subscribe</button></form></div>'
    source = '<article><h1>Report</h1>'+paragraph+widget+'<p>Following paragraph.</p></article>'
    cleaned, receipt = clean_registration_cards(source)
    assert paragraph in cleaned
    assert 'type="email"' not in cleaned
    assert [row['rule'] for row in receipt['removals']] == ['email_signup_subscription_component']


def test_shared_adjacent_cta_is_removed_and_recorded_once_for_two_components():
    widget = '<div class="subscribe-widget" data-component-name="SubscribeWidget"><form><input type="email"/><button type="submit">Subscribe</button></form></div>'
    cta = '<p><strong>By subscribing, you will join over 80,000 people who read this newsletter!</strong></p>'
    source = '<article><h1>Report</h1>'+widget+cta+widget+'</article>'
    cleaned, receipt = clean_registration_cards(source)
    assert '80,000' not in cleaned
    assert sum(row['rule'] == 'adjacent_subscription_promotion_paragraph' for row in receipt['removals']) == 1
    assert sum(row['rule'] == 'email_signup_subscription_component' for row in receipt['removals']) == 2


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



def test_article_opening_uses_dom_identity_not_feed_title_and_keeps_body_verbatim():
    from src.extractors.html_components import clean_article_opening
    title = "An entirely rewritten page headline"
    body = "The report starts here.\nA section heading\nFurther evidence remains."
    source = f'<article><h1>{title}</h1><details><summary>Contents</summary><nav><a href="#section">Jump</a></nav></details><p>The report starts here.</p><h2 id="section">A section heading</h2><p>Further evidence remains.</p></article>'
    text = title+'\nContents\n'+body
    cleaned, proof = clean_article_opening(text, source)
    assert cleaned == body
    assert [r['rule'] for r in proof['removals']] == ['article_primary_heading', 'article_table_of_contents_label']
    assert all(text[r['start']:r['end']] == r['text'] for r in proof['removals'])
    assert proof['output_sha256'] == hashlib.sha256(body.encode()).hexdigest()
    assert clean_article_opening(cleaned, source) == (cleaned, {})


@pytest.mark.parametrize('source', [
    '<main><h1>Title</h1><p>Keep body.</p></main>',
    '<article><h1>Title</h1><h1>Another title</h1><p>Keep body.</p></article>',
    '<article><h1>Different title</h1><p>Keep body.</p></article>',
    '<article><blockquote><h1>Title</h1></blockquote><p>Keep body.</p></article>',
])
def test_uncertain_or_quoted_headings_are_preserved(source):
    from src.extractors.html_components import clean_article_opening
    assert clean_article_opening('Title\nKeep body.', source) == ('Title\nKeep body.', {})


def test_non_navigation_details_and_article_prose_are_preserved():
    from src.extractors.html_components import clean_article_opening
    source = '<article><h1>Title</h1><details><summary>Important explanation</summary><p>Keep this explanation.</p></details><p>Body.</p></article>'
    text = 'Title\nImportant explanation\nKeep this explanation.\nBody.'
    assert clean_article_opening(text, source)[0] == text.split('\n',1)[1]


NEWSLETTER_PROMO = (
    'This is The Field Notes, a weekly newsletter breaking down one story from technology. '
    'For more about its impact, follow Alex Example. '
    'The Field Notes arrives in our subscribers’ inboxes on Sunday at 8AM. '
    'Opt in for The Field Notes here.'
)


def newsletter_paragraph(text=NEWSLETTER_PROMO, target='/newsletters'):
    return '<p><em>'+text.replace('here.', f'<a href="{target}">here</a>.')+'</em></p>'


def test_plain_newsletter_preamble_uses_dom_proof_and_preserves_every_other_character():
    from src.extractors.html_components import clean_newsletter_preambles
    body = 'Real reporting begins here.\nA section heading\nThe complete evidence remains.'
    source = '<article><h1>Report</h1>'+newsletter_paragraph()+'<p>Real reporting begins here.</p></article>'
    text = NEWSLETTER_PROMO+'\n'+body
    cleaned, proof = clean_newsletter_preambles(text, source)
    assert cleaned == body
    removal = proof['removals'][0]
    assert text[removal['start']:removal['end']] == removal['text']
    assert removal['target'] == '/newsletters'
    assert '<em>' in removal['serialized_html']
    assert clean_newsletter_preambles(cleaned, source) == (cleaned, {})
    assert clean_registration_cards(source) == (source, {})


@pytest.mark.parametrize('source', [
    '<article>'+newsletter_paragraph(NEWSLETTER_PROMO+' The publisher was acquired yesterday.')+'</article>',
    '<article><blockquote>'+newsletter_paragraph()+'</blockquote></article>',
    '<article><q>'+newsletter_paragraph()+'</q></article>',
    '<article><pre>'+newsletter_paragraph()+'</pre></article>',
    '<article><table><tr><td>'+newsletter_paragraph()+'</td></tr></table></article>',
    '<article>'+newsletter_paragraph(target='/report')+'</article>',
    '<article><p>The report explains how newsletters arrive in subscriber inboxes.</p></article>',
    '<article><p><em>Subscribe to our newsletter here.</em> A new study found traffic declined.</p></article>',
])
def test_plain_newsletter_reporting_quotes_and_uncertain_structure_are_preserved(source):
    from src.extractors.html_components import clean_newsletter_preambles
    text = NEWSLETTER_PROMO+'\nReporting remains.'
    assert clean_newsletter_preambles(text, source) == (text, {})


def test_same_newsletter_words_in_list_content_are_not_assigned_to_missing_ui_copies():
    from src.extractors.html_components import clean_newsletter_preambles
    source = '<article>'+newsletter_paragraph()*2+'<ul><li>'+NEWSLETTER_PROMO+'</li></ul></article>'
    text = NEWSLETTER_PROMO+'\n'+NEWSLETTER_PROMO+'\nReporting remains.'
    assert clean_newsletter_preambles(text, source) == (text, {})


def test_repeated_header_copies_anywhere_keep_one_standfirst_and_all_body_headings():
    from src.extractors.html_components import clean_article_opening
    header = '<div><h1>Page headline</h1><p>A distinct standfirst.</p></div>'
    source = '<article>'+header+header+'<h2>First section</h2><p>Body remains.</p></article>'
    text = 'An actual introduction.\nPage headline\nA distinct standfirst.\nPage headline\nA distinct standfirst.\nFirst section\nBody remains.'
    cleaned, proof = clean_article_opening(text, source)
    assert cleaned == 'An actual introduction.\nA distinct standfirst.\nFirst section\nBody remains.'
    for row in proof['removals']:
        assert text[row['start']:row['end']] == row['text']
    restored = text
    for row in reversed(proof['removals']):
        restored = restored[:row['start']]+restored[row['end']:]
    assert cleaned == restored
    assert clean_article_opening(cleaned, source) == (cleaned, {})


def test_sole_isolated_page_heading_after_intro_is_removed_without_losing_standfirst():
    from src.extractors.html_components import clean_article_opening
    source = '<article><div><h1>Page headline</h1><p>A distinct standfirst.</p></div><p>Actual introduction.</p></article>'
    text = 'Actual introduction.\nPage headline\nA distinct standfirst.'
    cleaned, proof = clean_article_opening(text, source)
    assert cleaned == 'Actual introduction.\nA distinct standfirst.'
    assert len(proof['removals']) == 1


def test_responsive_standfirst_outside_h1_wrapper_needs_shared_header_marker():
    from src.extractors.html_components import clean_article_opening
    source = ('<article><header><div><h1>Page headline</h1><p>A distinct standfirst.</p></div>'
              '<div><p>A distinct standfirst.</p></div></header><p>Actual introduction.</p></article>')
    text = 'Actual introduction.\nPage headline\nA distinct standfirst.\nA distinct standfirst.'
    assert clean_article_opening(text, source)[0] == 'Actual introduction.\nA distinct standfirst.\n'
    uncertain = source.replace('<header>', '<section>').replace('</header>', '</section>')
    assert clean_article_opening(text, uncertain)[0] == 'Actual introduction.\nA distinct standfirst.\nA distinct standfirst.'
    with_body_copy = source.replace('</article>', '<ul><li>A distinct standfirst.</li></ul></article>')
    assert clean_article_opening(text, with_body_copy)[0] == 'Actual introduction.\nA distinct standfirst.\nA distinct standfirst.'


@pytest.mark.parametrize('source', [
    '<article><div><h1>Headline</h1><p>One lead.</p></div><div><h1>Headline</h1><p>Another lead.</p></div></article>',
    '<article><div><h1>Headline</h1><p>One lead.</p><p>Extra reporting.</p></div><div><h1>Headline</h1><p>One lead.</p></div></article>',
    '<article><div><h1>Headline</h1><p>One lead.</p></div><div><h1>Headline</h1><p>One lead.</p></div><blockquote>Headline</blockquote></article>',
])
def test_different_or_mixed_headers_and_independent_quotes_are_preserved(source):
    from src.extractors.html_components import clean_article_opening
    text = 'Introduction.\nHeadline\nOne lead.\nHeadline\nOne lead.\nBody remains.'
    assert clean_article_opening(text, source) == (text, {})


def test_marked_product_listing_keeps_description_pricing_and_maker_evidence():
    from src.extractors.html_components import clean_product_listing_components
    source = '''<main><div data-test="header"><div>Launching today</div><h1>Example Product</h1>
<h2>A workspace for people and agents</h2><p>213 followers</p>
<h2>A workspace for people and agents</h2><p>213 followers</p>
<a data-test="visit-website-button" href="https://product.example">Visit</a>
<a data-test="product-navigation-item-overview" href="/overview">Overview</a>
<p>The product supports shared documents and version history.</p></div>
<div data-test="launch"><div>Launch Team</div><div><img alt="Other service"/>
<span>Other service</span><span>Get credits</span><button>Try it</button><span>Promoted</span></div>
<p>Free Options</p><p>The maker explained a concrete workflow.</p></div></main>'''
    text = ('Launching today\nExample Product\nA workspace for people and agents\n213 followers\n'
            'A workspace for people and agents\n213 followers\n'
            'The product supports shared documents and version history.\nFree Options\n'
            'Launch Team\nOther serviceGet credits\nPromoted\nThe maker explained a concrete workflow.')
    cleaned, proof = clean_product_listing_components(text, source)
    assert cleaned == ('The product supports shared documents and version history.\nFree Options\n'
                       'The maker explained a concrete workflow.')
    result = text
    for removal in reversed(proof['removals']):
        assert result[removal['start']:removal['end']] == removal['text']
        result = result[:removal['start']]+result[removal['end']:]
    assert result == cleaned
    assert clean_product_listing_components(cleaned, source) == (cleaned, {})
    assert clean_product_listing_components(text, source.replace('data-test=', 'data-example=')) == (text, {})
    quoted = source.replace('<main>', '<main><blockquote>').replace('</main>', '</blockquote></main>')
    assert clean_product_listing_components(text, quoted) == (text, {})
    same_description = source.replace('The product supports shared documents and version history.',
                                      'A workspace for people and agents')
    input_text = text.replace('The product supports shared documents and version history.',
                              'A workspace for people and agents')
    result, _ = clean_product_listing_components(input_text, same_description)
    assert result.count('A workspace for people and agents') == 3
    collapsed = input_text.replace('A workspace for people and agents\n213 followers\n', '', 1)
    result, _ = clean_product_listing_components(collapsed, same_description)
    assert result.count('A workspace for people and agents') == 2
    ambiguous_ad = source.replace('</main>', '<p>Other serviceGet credits</p></main>')
    assert 'Other serviceGet credits' in clean_product_listing_components(text, ambiguous_ad)[0]


def test_extractor_trims_primary_heading_but_keeps_original_body():
    source = '<html><body><article>'+STORY+'</article></body></html>'
    expected = trafilatura.extract(source).split('\n',1)[1]
    text, receipt = TrafilaturaExtractor(TrafilaturaExtractorConfig()).extract_text_with_receipt(source)
    assert text == expected
    assert receipt['article_opening']['removals'][0]['rule'] == 'article_primary_heading'


@pytest.mark.parametrize('name', ['lab', 'voxtral'])
def test_real_article_dom_recovers_lead_without_rewriting_or_losing_body(name):
    source = (Path(__file__).parent/'fixtures/article_content'/f'{name}.html').read_text()
    text, receipt = TrafilaturaExtractor(TrafilaturaExtractorConfig()).extract_text_with_receipt(source)
    assert receipt['article_content']['extracted_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    if name == 'lab':
        assert text.startswith('Gamow Labs builds frontier AI systems')
        assert 'To address this challenge, we present LabRobotBench.' in text
        assert 'GPT 6.1 SolCodex46.7%' in text  # Useful results stay in the full body.
        assert receipt['article_content']['adjustments'][0]['intro']
    else:
        # This reduced DOM retains the publisher's Summary heading; full-page
        # fallback selection is separately checked in the recorded URL replay.
        assert text.index('Voxtral 3 introduces a cutting-edge voice agent SDK') < 30
        assert 'Merci de recevoir' not in text
        assert 'book_appointment' not in text
        assert 'Speech transcription and voice generation have come a long way' in text
        assert len(receipt['article_content']['removals']) == 2
    cleaned, proof = prepare_article_content(source)
    assert proof['input_sha256'] == hashlib.sha256(source.encode()).hexdigest()
    assert proof['output_sha256'] == hashlib.sha256(cleaned.encode()).hexdigest()
    assert prepare_article_content(cleaned) == (cleaned, {})
    from lxml import html
    tree = html.document_fromstring(source)
    for removal in proof['removals']:
        assert tree.xpath(removal['xpath'])[0].text_content() == removal['text']


def live_steps():
    return ('<div aria-live="polite"><div data-index="0" data-duration="2000" aria-hidden="true">'
            '<p>A staged utterance.</p></div><div data-index="1" data-duration="3000" '
            'aria-hidden="true"><p>Another staged utterance.</p></div></div>')


@pytest.mark.parametrize('body', [
    '<div aria-live="polite"><p>A live news update.</p></div>',
    '<p>Agent: a static transcript. Tool call: an editorial explanation.</p>',
    '<blockquote>'+live_steps()+'</blockquote>',
    '<table><tr><td>'+live_steps()+'</td></tr></table>',
    '<pre><code>Agent / Tool call / Result</code></pre>',
    live_steps().replace('data-duration="2000"', 'data-duration="not-a-time"'),
    live_steps().replace('data-index="1"', 'data-index="0"'),
    live_steps().replace('aria-live="polite"', 'aria-live="off"'),
    live_steps().replace('<p>A staged utterance.</p>', '<h2>Editorial heading</h2>'),
])
def test_live_news_static_transcripts_quotes_tables_and_uncertain_ui_are_unchanged(body):
    source = '<article><h1>A report</h1>'+body+'<p>Actual report.</p></article>'
    assert prepare_article_content(source) == (source, {})


@pytest.mark.parametrize('source', [
    '<main><h1>Title</h1>'+live_steps()+'</main>',
    '<article><h1>One</h1><h1>Two</h1>'+live_steps()+'</article>',
    '<article><h1>One</h1>'+live_steps()+'</article><article>Other article</article>',
    '<article><h1>Title</h1><p><a href="#body">Contents</a></p><div class="post-body"><p>Body.</p></div></article>',
    '<article><h1>Title</h1><div class="post-body"><p>First paragraph.</p><p>Next paragraph.</p></div></article>',
    '<article><h1>Title</h1><header><p>By the author</p></header><div class="post-body"><p>Body.</p></div></article>',
])
def test_ambiguous_articles_and_inner_bodies_without_excluded_intro_are_unchanged(source):
    assert prepare_article_content(source) == (source, {})


def test_intro_already_retained_by_trafilatura_does_not_trigger_a_second_extraction():
    source = '<article><h1>Title</h1><p>The original lead.</p><div class="post-body"><p>Details.</p></div></article>'
    assert prepare_article_content(source, 'The original lead.\nDetails.') == (source, {})


def test_rss_forwards_article_content_receipt_and_recovered_body():
    source = RSSSourceConfig(name='Example', url='https://example.com/feed',
                             content_extractor='trafilatura', require_content_extraction=True)
    response = MagicMock()
    response.headers = {}
    response.text = (Path(__file__).parent/'fixtures/article_content/lab.html').read_text()
    scraper = RSSScraper([source], AsyncMock(), ExtractorRegistry({}))
    with patch('src.extractors.trafilatura.safe_request', new=AsyncMock(return_value=response)):
        item = asyncio.run(scraper._build_item(source, {'title':'LabRobotBench', 'link':'https://example.com/lab'},
                                              datetime.now(timezone.utc)))
    assert item.content.startswith('Gamow Labs builds')
    assert item.metadata['content_extraction']['html_cleaning']['article_content']['adjustments']


def test_adjacent_promotion_prefix_does_not_remove_an_additional_reporting_sentence():
    widget = '<div class="subscribe-widget" data-component-name="SubscribeWidget"><form><input type="email"/><button type="submit">Subscribe</button></form></div>'
    prose = '<p><strong>By subscribing, you will join our readers. The report explains a decline in newsletter revenue.</strong></p>'
    source = '<article><h1>Report</h1>'+widget+prose+'</article>'
    cleaned, _ = clean_registration_cards(source)
    assert 'The report explains a decline in newsletter revenue.' in cleaned
    assert 'By subscribing, you will join our readers.' in cleaned


@pytest.mark.parametrize('bom', ['\ufeff', ' \ufeff'])
def test_responsive_header_edge_bom_matches_extractor_without_losing_body(bom):
    from src.extractors.html_components import clean_article_opening
    lead = 'The previous reporting established the specific shopping behavior.'
    source = ('<article><div class="duet--article--lede"><div><h1>Headline</h1><p>'
              +lead+bom+'</p></div><div><p>'+lead+bom+'</p></div></div>'
              '<p>All independent reporting remains.</p></article>')
    text = 'Headline\n'+lead+'\n'+lead+'\nAll independent reporting remains.'
    cleaned, proof = clean_article_opening(text, source)
    assert cleaned == lead+'\nAll independent reporting remains.'
    rebuilt = text
    for removal in reversed(proof['removals']):
        assert text[removal['start']:removal['end']] == removal['text']
        rebuilt = rebuilt[:removal['start']]+rebuilt[removal['end']:]
    assert rebuilt == cleaned
    assert any(bom in row['serialized_html'] for row in proof['removals'])
    # A real body copy, even with an edge BOM, prevents ambiguous text deletion.
    ambiguous = source.replace('</article>', '<ul><li>'+lead+bom+'</li></ul></article>')
    assert clean_article_opening(text, ambiguous)[0].count(lead) == 2
    uncertain = source.replace('duet--article--lede', 'unmarked-wrapper')
    assert clean_article_opening(text, uncertain)[0].count(lead) == 2
