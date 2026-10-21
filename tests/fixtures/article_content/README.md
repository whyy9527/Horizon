# Article boundary regression DOMs

Reduced DOMs from public pages fetched on 2026-10-21:

- `lab.html`: https://gamowlabs.com/lab-robot-bench-v0-1 — real introduction
  outside the named `post-body`, followed by benchmark results and cases.
- `voxtral.html`: https://mistral.ai/news/voxtral-3/ — the real H1, two timed
  live dialogue regions, publisher summary and opening prose. Graphics and
  unrelated sections are omitted. This reduced document can retain Summary
  labels that the full-page extractor omits; it checks dialogue removal rather
  than reproducing the full page's fallback-selection output byte for byte.

Full responses, hashes, default/candidate output comparison, live URL receipts
and excerpt provenance remain in the AI Learning validation record referenced
by issue-0067. These fixtures test DOM boundaries without network access; they
are not alternative article text supplied to the daily pipeline.
