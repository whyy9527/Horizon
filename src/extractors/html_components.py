"""Conservative removal of standalone registration cards before text extraction."""

import hashlib
from copy import deepcopy
from urllib.parse import urlsplit

from lxml import html as lxml_html


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
