"""Conservative DOM boundaries and component handling for article extraction."""

import hashlib
import re
from copy import deepcopy
from urllib.parse import urlsplit

from lxml import html as lxml_html


SUBSCRIBE_PROMO_PATTERNS = (
    re.compile(r"^thanks\s+for\s+reading!\s+(?:if\s+you\s+haven['’]t\s+subscribed,\s*"
               r"(?:please\s+)?click\s+(?:the\s+)?(?:button|link)\s+below:?|"
               r"to\s+receive\s+[^.!?]+,\s*consider\s+subscribing)[!.]*$", re.IGNORECASE),
    re.compile(r"^by\s+subscribing,\s*(?:you(?:['’]ll|\s+will)\s+)?"
               r"(?:join|receive)\s+[^.!?]+(?:readers|people\s+who\s+read\s+[^.!?]+|new\s+posts|updates|newsletter)[.!]$",
               re.IGNORECASE),
)
NEWSLETTER_PREAMBLE = re.compile(
    r"^This\s+is\s+[^.!?]+,\s+(?:a|our)\s+[^.!?]*\bnewsletter\b[^.!?]*[.!]\s+"
    r"(?:For\s+more\s+[^.!?]+,\s+follow\s+[^.!?]+[.!]\s+)?"
    r"[^.!?]+\s+(?:arrives?|lands?|is\s+delivered)\s+in\s+(?:our\s+)?"
    r"subscribers?['’]?\s+inboxes?\s+[^.!?]+[.!]\s+"
    r"(?:Opt\s+in|Sign\s+up|Subscribe)\s+(?:for|to)\s+[^.!?]+[.!]$",
    re.IGNORECASE,
)
EDITORIAL_STRUCTURES = (".//blockquote|.//q|.//cite|.//pre|.//code|.//table|.//figure|.//article|"
                        ".//h1|.//h2|.//h3|.//h4|.//h5|.//h6")


def _has_email_signup_form(node) -> bool:
    return bool(node.xpath(
        ".//form[.//input[translate(@type, 'EMAIL', 'email')='email'] "
        "and (.//button[translate(@type, 'SUBMIT', 'submit')='submit'] "
        "or .//input[translate(@type, 'SUBMIT', 'submit')='submit'])]"
    ))


def _is_subscription_component(node) -> bool:
    if not isinstance(node.tag, str):
        return False
    classes = set((node.get("class") or "").split())
    marked = (node.get("data-component-name") == "SubscribeWidget"
              or classes.intersection({"subscribe-widget", "subscription-widget",
                                       "subscription-widget-wrap"}))
    return bool(marked and _has_email_signup_form(node)
                and not node.xpath("ancestor::blockquote|ancestor::q|ancestor::cite|ancestor::pre|ancestor::code|ancestor::table|ancestor::figure"))


def _safe_subscription_component(node) -> bool:
    """Refuse to remove a marked container that also carries article material."""
    if node.xpath(EDITORIAL_STRUCTURES):
        return False
    forms = set(node.xpath(".//form"))
    preambles = [candidate for candidate in node.iter()
                 if isinstance(candidate.tag, str)
                 and "preamble" in (candidate.get("class") or "").split()]
    promo_paragraphs = [paragraph for paragraph in node.xpath(".//p")
                        if any(ancestor in preambles for ancestor in paragraph.iterancestors())
                        and _is_preamble_promotion(paragraph)]
    for paragraph in node.xpath(".//p"):
        if not any(ancestor in preambles for ancestor in paragraph.iterancestors()):
            return False
        if not _is_preamble_promotion(paragraph):
            return False
    for element in node.iter():
        if not isinstance(element.tag, str):
            continue
        if (element.text or "").strip():
            if not any(form is element or form in element.iterancestors() for form in forms):
                if not any(paragraph is element or paragraph in element.iterancestors()
                           for paragraph in promo_paragraphs):
                    return False
        if (element.tail or "").strip():
            parent = element.getparent()
            if parent is not None and not any(form is parent or form in parent.iterancestors()
                                              for form in forms):
                if not any(paragraph is parent or paragraph in parent.iterancestors()
                           for paragraph in promo_paragraphs):
                    return False
    return True


def _is_explicit_subscription_promotion(node) -> bool:
    if (node.tag != "p"
            or node.xpath(".//pre|.//code|.//blockquote|.//q|.//cite")
            or node.xpath("ancestor::blockquote|ancestor::q|ancestor::cite|ancestor::pre|ancestor::code|ancestor::table|ancestor::figure")):
        return False
    text = " ".join(node.text_content().split())
    emphasized = [child for child in node.xpath(".//strong|.//b")
                  if " ".join(child.text_content().split()) == text]
    return bool(text and emphasized and _matches_subscription_promotion(text))


def _matches_subscription_promotion(text: str) -> bool:
    return any(pattern.fullmatch(text) for pattern in SUBSCRIBE_PROMO_PATTERNS)


def _is_preamble_promotion(node) -> bool:
    if (node.tag != "p"
            or node.xpath(".//pre|.//code|.//blockquote|.//q|.//cite")
            or node.xpath("ancestor::blockquote|ancestor::q|ancestor::cite|ancestor::pre|ancestor::code|ancestor::table|ancestor::figure")):
        return False
    return _matches_subscription_promotion(" ".join(node.text_content().split()))


def _node_proof(node, xpath: str, rule: str, **extra) -> dict:
    serialized = lxml_html.tostring(node, encoding="unicode", with_tail=False)
    return {"rule": rule, "xpath": xpath, "text": node.text_content(),
            "serialized_html": serialized, **extra}


def _has_unowned_text_copy(tree, value, owned):
    """A matching prose/list/heading block outside the proven component is ambiguous."""
    blocks = tree.xpath('.//p|.//h1|.//h2|.//h3|.//h4|.//h5|.//h6|.//li|.//blockquote|.//q|.//cite|.//pre|.//code|'
                        './/div[not(.//a or .//button)]')
    for node in blocks:
        if ' '.join(node.text_content().split()) != value:
            continue
        if (node.xpath('ancestor::a|.//a')
                and not ''.join(node.xpath('.//text()[not(ancestor::a)]')).strip()):
            continue  # A related-story link is not an independent prose copy.
        if not any(node is candidate or node in candidate.iterancestors()
                   or candidate in node.iterancestors() for candidate in owned):
            return True
    return False


def _newsletter_promotion_link(node):
    """Recognize a complete publisher preamble, never a mixed reporting paragraph."""
    if (node.tag != 'p' or not node.xpath('.//em|.//i')
            or node.xpath(EDITORIAL_STRUCTURES)
            or node.xpath('ancestor::blockquote|ancestor::q|ancestor::cite|ancestor::pre|'
                          'ancestor::code|ancestor::table|ancestor::figure')):
        return None
    text = ' '.join(node.text_content().split())
    if not NEWSLETTER_PREAMBLE.fullmatch(text):
        return None
    links = node.xpath('.//a[@href]')
    if not links:
        return None
    link = links[-1]
    try:
        target = urlsplit(link.get('href'))
    except ValueError:
        return None
    if (target.scheme not in {'', 'http', 'https'}
            or target.path.rstrip('/').lower().split('/')[-1]
            not in {'newsletters', 'newsletter', 'subscribe', 'signup', 'sign-up'}
            or not ' '.join(link.text_content().split())):
        return None
    return link.get('href')


def clean_newsletter_preambles(text: str, source: str) -> tuple[str, dict]:
    """Remove complete DOM-proven promotion lines without changing body selection."""
    tree = lxml_html.fromstring(source)
    candidates = {}
    for paragraph in tree.xpath('//p'):
        target = _newsletter_promotion_link(paragraph)
        if target:
            value = ' '.join(paragraph.text_content().split())
            candidates.setdefault(value, []).append((paragraph, target))
    lines = text.splitlines(keepends=True)
    for value in list(candidates):
        if (sum(' '.join(line.split()) == value for line in lines) != len(candidates[value])
                or _has_unowned_text_copy(tree, value, [node for node, _ in candidates[value]])):
            del candidates[value]  # Do not confuse an independent quotation with UI.
    offset, kept, removals = 0, [], []
    for line in lines:
        matches = candidates.get(' '.join(line.split()))
        if matches:
            node, target = matches.pop(0)
            removals.append(_node_proof(
                node, tree.getroottree().getpath(node), 'standalone_newsletter_promotion_paragraph',
                start=offset, end=offset+len(line), text=line, target=target,
            ))
        else:
            kept.append(line)
        offset += len(line)
    cleaned = ''.join(kept)
    if not removals or not cleaned.strip():
        return text, {}
    return cleaned, {'input_sha256': hashlib.sha256(text.encode()).hexdigest(),
                     'output_sha256': hashlib.sha256(cleaned.encode()).hexdigest(),
                     'removals': removals}


def clean_product_listing_components(text: str, source: str) -> tuple[str, dict]:
    """Remove proven listing chrome from text without changing body extraction."""
    tree = lxml_html.fromstring(source)
    headers = tree.xpath('//main//*[@data-test="header"][.//a[@data-test="visit-website-button"]'
                         ' and .//a[@data-test="product-navigation-item-overview"]]')
    launches = tree.xpath('//main//*[@data-test="launch"]')
    if len(headers) != 1 or len(launches) != 1:
        return text, {}
    header, launch = headers[0], launches[0]
    protected = ('ancestor::blockquote|ancestor::q|ancestor::cite|ancestor::pre|'
                 'ancestor::code|ancestor::table|ancestor::figure')
    if header.xpath(protected) or launch.xpath(protected):
        return text, {}
    titles, taglines = header.xpath('.//h1'), header.xpath('.//h2')
    if len(titles) != 1 or not taglines or len({' '.join(n.text_content().split()) for n in taglines}) != 1:
        return text, {}
    prefix_nodes = [*titles, *taglines,
                    *[p for p in header.xpath('.//p')
                      if re.fullmatch(r'[\d,.]+\s+followers?', ' '.join(p.text_content().split()), re.I)],
                    *header.xpath('.//div[not(*) and normalize-space(text())="Launching today"]')]
    prefix = {}
    for node in prefix_nodes:
        prefix.setdefault(' '.join(node.text_content().split()), []).append(node)
    ambiguous_prefix = {value for value in prefix if _has_unowned_text_copy(tree, value, prefix[value])}
    controls = {}
    for node in launch.xpath('.//div[not(*) and normalize-space(text())="Launch Team"]'):
        if not node.xpath(protected):
            controls[' '.join(node.text_content().split())] = (node, 'product_listing_team_control')
    for label in launch.xpath('.//span[normalize-space(text())="Promoted"]'):
        if label.xpath(protected):
            continue
        for card in label.iterancestors():
            if card is launch:
                break
            if (len(card.xpath('.//img[@alt]')) == 1 and len(card.xpath('.//button')) == 1
                    and not card.xpath(EDITORIAL_STRUCTURES)):
                snapshot = deepcopy(card)
                for node in snapshot.xpath('.//button|.//span[normalize-space(text())="Promoted"]'):
                    node.drop_tree()
                value = ' '.join(snapshot.text_content().split())
                if value:
                    controls[value] = (card, 'marked_product_listing_promotion')
                    controls['Promoted'] = (label, 'product_listing_promotion_label')
                break
    lines = text.splitlines(keepends=True)
    controls = {value: row for value, row in controls.items()
                if sum(' '.join(line.split()) == value for line in lines) == 1
                and not _has_unowned_text_copy(tree, value, [row[0]])}
    offset, in_prefix, kept, removals = 0, True, [], []
    for line in lines:
        value = ' '.join(line.split())
        candidates = prefix.get(value) if in_prefix else None
        if candidates:
            node, rule = candidates.pop(0), 'product_listing_identity_control'
            if value in ambiguous_prefix:
                node = None
        else:
            if value:
                in_prefix = False
            node, rule = controls.get(value, (None, None))
        if node is None:
            kept.append(line)
        else:
            removals.append(_node_proof(node, tree.getroottree().getpath(node), rule,
                                       start=offset, end=offset+len(line), text=line))
        offset += len(line)
    cleaned = ''.join(kept)
    if not removals or not cleaned.strip():
        return text, {}
    return cleaned, {'input_sha256': hashlib.sha256(text.encode()).hexdigest(),
                     'output_sha256': hashlib.sha256(cleaned.encode()).hexdigest(),
                     'removals': removals}


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
    """Remove proven signup UI and standalone cards without rewriting prose.

    Return original HTML unchanged when no independently identified card is
    removed. Subscription components may occur inside article bodies, so they
    are identified by their component marker plus a working email signup form.
    Only adjacent paragraphs with explicit subscribe-call language are included.
    """
    from trafilatura.readability_lxml import Document

    tree = lxml_html.fromstring(source)
    original_paths = {node: tree.getroottree().getpath(node) for node in tree.iter()
                      if isinstance(node.tag, str)}
    removals = []

    # Substack-style inline forms are sometimes emitted inside the extracted
    # article body. Require both a component marker and an email/submit form;
    # ignore examples and quotations where form-like markup is editorial.
    marked = [node for node in tree.iter() if _is_subscription_component(node)]
    safe_marked = [node for node in marked if _safe_subscription_component(node)]
    components = [node for node in safe_marked
                  if not any(ancestor in safe_marked for ancestor in node.iterancestors())]
    subscription_nodes = set()
    recorded_promotion_paragraphs = set()
    for component in marked:
        if component in safe_marked:
            continue
        classes = set((component.get("class") or "").split())
        if not classes.intersection({"subscription-widget", "subscription-widget-wrap"}):
            continue
        for paragraph in component.xpath(
                ".//*[contains(concat(' ',normalize-space(@class),' '),' preamble ')]//p"):
            if paragraph in recorded_promotion_paragraphs or not _is_preamble_promotion(paragraph):
                continue
            promo_text = " ".join(paragraph.text_content().split())
            matched = next(pattern.fullmatch(promo_text) for pattern in SUBSCRIBE_PROMO_PATTERNS
                           if pattern.fullmatch(promo_text))
            removals.append(_node_proof(
                paragraph, original_paths.get(paragraph, ""),
                "subscription_preamble_promotion_paragraph",
                component_xpath=original_paths[component],
                evidence="full subscribe call-to-action inside a preamble component",
                matched_text=matched.group(0),
            ))
            recorded_promotion_paragraphs.add(paragraph)
            subscription_nodes.add(paragraph)
    for component in components:
        component_path = original_paths[component]
        adjacent = []
        for direction in ("getprevious", "getnext"):
            sibling = getattr(component, direction)()
            while sibling is not None and not isinstance(sibling.tag, str):
                sibling = getattr(sibling, direction)()
            if sibling is not None and _is_explicit_subscription_promotion(sibling):
                adjacent.append(sibling)
        for paragraph in adjacent:
            if paragraph not in recorded_promotion_paragraphs:
                promo_text = " ".join(paragraph.text_content().split())
                matched = next(pattern.fullmatch(promo_text) for pattern in SUBSCRIBE_PROMO_PATTERNS
                               if pattern.fullmatch(promo_text))
                removals.append(_node_proof(
                    paragraph, original_paths.get(paragraph, ""),
                    "adjacent_subscription_promotion_paragraph",
                    component_xpath=component_path,
                    evidence="full emphasized subscribe call-to-action paragraph",
                    matched_text=matched.group(0),
                ))
                recorded_promotion_paragraphs.add(paragraph)
            subscription_nodes.add(paragraph)
        removals.append(_node_proof(
            component, component_path, "email_signup_subscription_component",
            evidence={"component_marker": component.get("data-component-name") or
                      sorted(set((component.get("class") or "").split()).intersection(
                          {"subscribe-widget", "subscription-widget", "subscription-widget-wrap"})),
                      "email_input": True, "submit_control": True},
        ))
        subscription_nodes.add(component)
    for node in sorted(subscription_nodes, key=lambda n: len(list(n.iterancestors())), reverse=True):
        node.drop_tree()

    attribute = "data-horizon-cleaning-node"
    originals = {}
    for index, node in enumerate(tree.iter()):
        if isinstance(node.tag, str):
            originals[node] = node.get(attribute)
            node.set(attribute, str(index))
    selected = lxml_html.fromstring(Document(deepcopy(tree)).summary())
    kept = set(selected.xpath(f"//@{attribute}"))
    if not kept:
        for node, value in originals.items():
            if value is None:
                node.attrib.pop(attribute, None)
            else:
                node.set(attribute, value)
        if not removals:
            return source, {}
        cleaned = lxml_html.tostring(tree, encoding="unicode")
        return cleaned, {"input_sha256": hashlib.sha256(source.encode()).hexdigest(),
                         "output_sha256": hashlib.sha256(cleaned.encode()).hexdigest(),
                         "removals": removals}

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
        removals.append(_node_proof(snapshot, original_paths.get(card, original_path),
                                    "standalone_registration_card_outside_main_content",
                                    target=link.get("href")))
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


def _clean_isolated_article_headers(text, tree, article, headings):
    """Remove page headings only when every H1 has the same isolated header.

    Work on the extractor's text so pruning a header cannot change its body
    selector or silently drop section headings. Retain the first standfirst.
    """
    parents = [heading.getparent() for heading in headings]
    if len(set(parents)) != len(headings) or len({len(list(p.iterancestors())) for p in parents}) != 1:
        return text, {}
    signatures, standfirsts = [], []
    for parent in parents:
        children = [n for n in parent if isinstance(n.tag, str)]
        if ([n.tag for n in children] != ['h1', 'p'] or (parent.text or '').strip()
                or any((n.tail or '').strip() for n in children)
                or children[1].xpath(EDITORIAL_STRUCTURES)):
            return text, {}
        signatures.append(tuple(' '.join(n.text_content().split()) for n in children))
        standfirsts.append(children[1])
    if len(set(signatures)) != 1 or not all(signatures[0]) or signatures[0][0] == signatures[0][1]:
        return text, {}
    title, subtitle = signatures[0]
    header_containers = [next((node for node in heading.iterancestors()
                               if article in node.iterancestors() and
                               (node.tag == 'header' or node.get('data-component-name') == 'ArticleHeader'
                                or 'duet--article--lede' in (node.get('class') or '').split())), None)
                         for heading in headings]
    if header_containers[0] is not None and len(set(header_containers)) == 1:
        # A responsive standfirst can be outside the H1's immediate wrapper,
        # while still belonging to the same explicitly marked article header.
        for node in header_containers[0].xpath('.//p'):
            if (node not in standfirsts and ' '.join(node.text_content().split()) == subtitle
                    and not node.xpath('ancestor::blockquote|ancestor::q|ancestor::cite|ancestor::pre|'
                                       'ancestor::code|ancestor::table|ancestor::figure')):
                standfirsts.append(node)
    dedupe_subtitle = True
    for node in article.xpath('.//p|.//blockquote|.//q|.//cite|.//pre|.//code'):
        if node not in standfirsts:
            value = ' '.join(node.text_content().split())
            if value == title:
                return text, {}  # An independent prose/quote occurrence is ambiguous.
            if value == subtitle:
                dedupe_subtitle = False
    if _has_unowned_text_copy(article, title, headings):
        return text, {}
    if _has_unowned_text_copy(article, subtitle, standfirsts):
        dedupe_subtitle = False
    lines = text.splitlines(keepends=True)
    counts = {value: sum(' '.join(line.split()) == value for line in lines)
              for value in (title, subtitle)}
    if counts[title] > len(headings):
        return text, {}
    dedupe_subtitle = dedupe_subtitle and counts[subtitle] <= len(standfirsts)
    offset, seen_subtitles, seen_titles = 0, 0, 0
    kept, removals = [], []
    for line in lines:
        value = ' '.join(line.split())
        node, rule = None, None
        if value == title:
            node, rule = headings[seen_titles], 'isolated_article_primary_heading'
            seen_titles += 1
        elif value == subtitle:
            if seen_subtitles and dedupe_subtitle:
                node, rule = standfirsts[seen_subtitles], 'repeated_article_standfirst'
            seen_subtitles += 1
        if node is None:
            kept.append(line)
        else:
            removals.append(_node_proof(node, tree.getroottree().getpath(node), rule,
                                       start=offset, end=offset+len(line), text=line))
        offset += len(line)
    cleaned = ''.join(kept)
    if not removals or not cleaned.strip():
        return text, {}
    return cleaned, {'input_sha256': hashlib.sha256(text.encode()).hexdigest(),
                     'output_sha256': hashlib.sha256(cleaned.encode()).hexdigest(),
                     'removals': removals}


def clean_article_opening(text: str, source: str) -> tuple[str, dict]:
    """Remove DOM-proven page headings, leaving article prose untouched.

    A rewritten feed title need not match the page's H1. The sole H1 in an
    explicit article identifies it instead. No fuzzy text or publisher rules.
    """
    tree = lxml_html.fromstring(source)
    articles = tree.xpath("//article")
    if len(articles) != 1:
        return text, {}
    article = articles[0]
    headings = article.xpath(".//h1[not(ancestor::blockquote or ancestor::q or ancestor::cite "
                             "or ancestor::pre or ancestor::code or ancestor::table or ancestor::figure)]")
    if headings:
        cleaned, proof = _clean_isolated_article_headers(text, tree, article, headings)
        if proof:
            return cleaned, proof
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
