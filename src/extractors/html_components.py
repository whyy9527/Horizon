"""Conservative DOM boundaries and component handling for article extraction."""

import hashlib
from copy import deepcopy
from urllib.parse import urlsplit

from lxml import html as lxml_html


def prepare_article_content(source: str, extracted_text: str = '') -> tuple[str, dict]:
    """Respect an unambiguous article boundary and omit timed live UI steps.

    This is an HTML adapter for the pinned Trafilatura selector order, not a
    replacement text extractor. No prose is rewritten or appended to its output.
    """
    from trafilatura.xpaths import BODY_XPATH

    tree = lxml_html.document_fromstring(source)
    articles = tree.xpath('//article')
    if len(articles) != 1:
        return source, {}
    article = articles[0]
    headings = article.xpath('.//h1[not(ancestor::blockquote or ancestor::pre or ancestor::code)]')
    if len(headings) != 1:
        return source, {}
    paths = {node: tree.getroottree().getpath(node) for node in article.iter()}
    removals, adjustments = [], []
    for node in article.xpath('.//*[@aria-live="polite" or @aria-live="assertive"]'):
        # Live news, static transcripts and code examples are editorial content.
        # Require multiple explicitly timed, indexed, hidden animation states.
        if node.xpath('ancestor::pre|ancestor::code|ancestor::blockquote|ancestor::table|'
                      './/pre|.//code|.//blockquote|.//table|.//h1|.//h2'):
            continue
        steps = node.xpath('.//*[@data-index and @data-duration and @aria-hidden="true"]')
        if (len({step.get('data-index') for step in steps}) < 2
                or not all(step.get('data-index').isdigit()
                           and step.get('data-duration').isdigit()
                           and int(step.get('data-duration')) > 0 for step in steps)):
            continue
        if any(parent in {r[0] for r in removals} for parent in node.iterancestors()):
            continue
        removals.append((node, {'rule': 'timed_live_interface', 'xpath': paths[node],
                               'text': node.text_content(),
                               'serialized_html': lxml_html.tostring(node, encoding='unicode', with_tail=False)}))

    # Trafilatura tries named inner bodies before <article>. An author-supplied
    # article can contain a real introduction outside that narrower container.
    matches = BODY_XPATH[0](tree)
    if matches and article in matches[0].iterancestors():
        selected = matches[0]
        order = list(article.iter())
        heading_pos, body_pos = order.index(headings[0]), order.index(selected)
        intro = [node for node in order[heading_pos+1:body_pos] if node.tag == 'p'
                 and not node.xpath('ancestor::header|ancestor::nav|ancestor::footer|'
                                    'ancestor::aside|ancestor::figure|ancestor::blockquote|'
                                    'ancestor::pre|ancestor::code|ancestor::*[@aria-hidden="true" or @hidden]')
                 and not any(parent in {r[0] for r in removals} for parent in node.iterancestors())
                 and ''.join(node.xpath('.//text()[not(ancestor::a or ancestor::time)]')).strip()]
        normalized_text = ' '.join(extracted_text.split())
        missing_intro = [node for node in intro
                         if ' '.join(node.text_content().split()) not in normalized_text]
        if heading_pos < body_pos and missing_intro:
            adjustments.append({'rule': 'explicit_article_includes_intro', 'xpath': paths[article],
                                'selected_body_xpath': paths[selected],
                                'previous_itemprop': article.get('itemprop'),
                                'intro': [{'xpath': paths[node], 'text': node.text_content(),
                                           'serialized_html': lxml_html.tostring(node, encoding='unicode', with_tail=False)}
                                          for node in missing_intro]})
            article.set('itemprop', 'articleBody')

    if not removals and not adjustments:
        return source, {}
    for node, _ in removals:
        node.drop_tree()
    cleaned = lxml_html.tostring(tree, encoding='unicode')
    return cleaned, {'input_sha256': hashlib.sha256(source.encode()).hexdigest(),
                     'output_sha256': hashlib.sha256(cleaned.encode()).hexdigest(),
                     'removals': [proof for _, proof in removals], 'adjustments': adjustments}


def clean_registration_cards(source: str) -> tuple[str, dict]:
    """Use Readability only as a protection boundary, never as replacement text.

    Return original HTML unchanged when no independently identified card is
    removed. In particular, tables and intro text outside Readability survive.
    """
    from trafilatura.readability_lxml import Document

    tree = lxml_html.fromstring(source)
    attribute = "data-horizon-cleaning-node"
    originals = {}
    for index, node in enumerate(tree.iter()):
        if isinstance(node.tag, str):
            originals[node] = node.get(attribute)
            node.set(attribute, str(index))
    selected = lxml_html.fromstring(Document(deepcopy(tree)).summary())
    kept = set(selected.xpath(f"//@{attribute}"))
    if not kept:
        return source, {}

    removals = []
    links = [(link, tree.getroottree().getpath(link.getparent()))
             for link in tree.xpath("//a[@href]")]
    for link, original_path in links:
        try:
            parsed = urlsplit(link.get("href"))
        except ValueError:
            continue
        if parsed.scheme not in {"", "http", "https"}:
            continue
        target = parsed.path.rstrip("/").lower()
        if target not in {"/signup", "/sign-up", "/register", "/registration"}:
            continue
        card = link.getparent()
        children = [child for child in card if isinstance(child.tag, str)]
        if len(children) != 3 or children[0].tag not in {"h2", "h3", "h4", "h5", "h6"}:
            continue
        if [child.tag for child in children[1:]] != ["p", "a"]:
            continue
        if (card.text or "").strip() or any((child.tail or "").strip() for child in children):
            continue
        if card.xpath(".//table|.//pre|.//code|.//blockquote|.//article"):
            continue
        if any(node.get(attribute) in kept for node in card.iter()):
            continue
        if any(node.tag == "article" or "articleBody" in node.get("itemprop", "").split()
               for node in (card, *card.iterancestors())):
            continue
        # Record the original DOM path and normalized HTML, without claiming
        # character offsets into a differently serialized source document.
        snapshot = deepcopy(card)
        for node in snapshot.iter():
            node.attrib.pop(attribute, None)
        removals.append({
            "rule": "standalone_registration_card_outside_main_content",
            "xpath": original_path,
            "text": card.text_content(),
            "target": link.get("href"),
            "serialized_html": lxml_html.tostring(snapshot, encoding="unicode", with_tail=False),
        })
        card.drop_tree()

    if not removals:
        return source, {}
    for node, value in originals.items():
        if value is None:
            node.attrib.pop(attribute, None)
        else:
            node.set(attribute, value)
    cleaned = lxml_html.tostring(tree, encoding="unicode")
    return cleaned, {
        "input_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "output_sha256": hashlib.sha256(cleaned.encode()).hexdigest(),
        "removals": removals,
    }


def clean_article_opening(text: str, source: str) -> tuple[str, dict]:
    """Trim only a DOM-proven title/TOC prefix, leaving article prose untouched.

    A rewritten feed title need not match the page's H1. The sole H1 in an
    explicit article identifies it instead. No fuzzy text or publisher rules.
    """
    tree = lxml_html.fromstring(source)
    articles = tree.xpath("//article")
    if len(articles) != 1:
        return text, {}
    article = articles[0]
    headings = article.xpath(".//h1[not(ancestor::blockquote or ancestor::pre or ancestor::code)]")
    if len(headings) != 1:
        return text, {}
    candidates = [(headings[0], "article_primary_heading")]
    for details in article.xpath(".//details"):
        children = [n for n in details if isinstance(n.tag, str)]
        if [n.tag for n in children] != ["summary", "nav"]:
            continue
        nav = children[1]
        links = nav.xpath(".//a[@href]")
        if (links and not details.xpath(".//pre|.//code|.//blockquote")
                and all(a.get("href").startswith("#") and len(a.get("href")) > 1
                        and tree.xpath("//*[@id=$target]", target=a.get("href")[1:]) for a in links)):
            candidates.append((children[0], "article_table_of_contents_label"))
    offset, removals = 0, []
    for node, rule in candidates:
        start = offset
        while start < len(text) and text[start].isspace():
            start += 1
        end = text.find("\n", start)
        if end < 0:
            continue  # A title alone is not a usable article body.
        expected = " ".join(node.text_content().split())
        if not expected or " ".join(text[start:end].split()) != expected:
            continue
        offset = end + 1
        removals.append({"rule": rule, "xpath": tree.getroottree().getpath(node),
                         "start": start, "end": end, "text": text[start:end],
                         "serialized_html": lxml_html.tostring(node, encoding="unicode", with_tail=False)})
    if not removals or not text[offset:].strip():
        return text, {}
    cleaned = text[offset:].lstrip()
    return cleaned, {"input_sha256": hashlib.sha256(text.encode()).hexdigest(),
                     "output_sha256": hashlib.sha256(cleaned.encode()).hexdigest(),
                     "removals": removals}
