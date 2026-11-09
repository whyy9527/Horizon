import hashlib

import pytest

from src.extractors.html_components import clean_article_opening


@pytest.mark.parametrize('minutes,title,body', [
    (3, 'NASA Navigation Experiment Shows Promise for Planetary Landings',
     'NASA successfully demonstrated technology designed to help spacecraft autonomously navigate.'),
    (4, 'NASA Shares Plans to Accelerate Artemis, Return to Moon',
     'In an update on Oct. 23, NASA leaders shared the path to returning to the lunar surface.'),
])
def test_removes_dom_proven_reading_time_and_heading_prefix(minutes, title, body):
    source = (f'<article><div><p class="label carbon-60">{minutes} min read</p>'
              f'<h1>{title}</h1></div><p>{body}</p></article>')
    original = f'{minutes} min read\n{title}\n{body}'

    cleaned, receipt = clean_article_opening(original, source)

    assert cleaned == body
    assert original.endswith(cleaned)
    assert receipt['input_sha256'] == hashlib.sha256(original.encode()).hexdigest()
    assert receipt['output_sha256'] == hashlib.sha256(body.encode()).hexdigest()
    assert [removal['rule'] for removal in receipt['removals']] == [
        'article_reading_time_label', 'article_primary_heading']
    for removal in receipt['removals']:
        assert original[removal['start']:removal['end']] == removal['text']
        assert removal['xpath'] and removal['serialized_html']


@pytest.mark.parametrize('source,text', [
    # Ordinary prose next to a heading is not reading metadata.
    ('<article><div><p>3 min read</p><h1>Title</h1></div><p>Body.</p></article>',
     '3 min read\nTitle\nBody.'),
    ('<article><div><p class="label">An editorial introduction.</p><h1>Title</h1></div>'
     '<p>Body.</p></article>', 'An editorial introduction.\nTitle\nBody.'),
    # The header must be isolated, and both extracted lines must match its DOM.
    ('<article><div><p class="label">3 min read</p><h1>Title</h1><p>Intro.</p></div>'
     '<p>Body.</p></article>', '3 min read\nTitle\nIntro.\nBody.'),
    ('<article><div><p class="label">3 min read</p><h1>Title</h1></div><p>Body.</p></article>',
     '3 min read\nA different heading\nBody.'),
])
def test_leaves_unproven_or_unmatched_reading_time_prefix_untouched(source, text):
    assert clean_article_opening(text, source) == (text, {})


def test_preserves_reading_time_in_prose_after_normal_heading():
    source = ('<article><div><p class="label">3 min read</p><h1>Title</h1></div>'
              '<p>3 min read is the phrase being discussed.</p></article>')
    cleaned, _ = clean_article_opening('Title\n3 min read is the phrase being discussed.', source)
    assert cleaned == '3 min read is the phrase being discussed.'
