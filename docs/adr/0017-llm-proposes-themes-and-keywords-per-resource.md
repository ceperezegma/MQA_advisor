---
status: accepted
---

# An LLM enriches the themes and keywords of each Dataset and Data Service

The advisor's first recommendation feature: for each Dataset and standalone Data Service, propose
more `dcat:theme` and `dcat:keyword` values, from its title and description only. The design
document is `docs/design/theme-keyword-proposals.md`; this ADR records the decisions that are hard
to reverse or surprising.

## Decision

- **Scope.** Datasets and standalone Data Services (rows of `resource`). Distributions have neither
  property in the MQA methodology, and embedded Data Services are not resources, so both are out.
  `dct:temporal` was considered and dropped: only `theme` and `keyword`.
- **Enrichment, not gap filling.** A proposal is an addition. It is requested even when the
  resource already has values, as long as the total stays under a cap (5 themes, 10 keywords).
  Existing values are never changed or removed, and out-of-vocabulary existing themes are left
  alone (they simply do not count toward the cap or serve as context).
- **Two calls per resource, themes first, then keywords.**
  - **Themes call (`propose_themes`).** Input: the title, the description and the entries of the
    EU `data-theme` vocabulary (`vocabularies/data-theme-skos.rdf`). The model answers with
    vocabulary codes. Code validates every returned code against the vocabulary and drops
    duplicates of the resource's existing themes. It runs when the resource has fewer than 5
    in-vocabulary themes, and asks for between 1 and (5 - existing) new ones.
  - **Keywords call (`propose_keywords`).** Input: the title, the description and the labels of
    the *effective themes* (the existing in-vocabulary themes plus the ones the themes call just
    proposed) as extra context. The model answers with free-text keywords. Code trims them, drops
    empty ones and duplicates (case-insensitive) of the existing or already proposed keywords,
    and keeps the case as returned. It runs when the resource has fewer than 10 keywords of any
    language, and asks for between 1 and (10 - existing) new ones.
  - **Both calls.** Each returns at least one new value. An empty result after validation is
    retried (3 times at most) and then reported as an error. If the themes call fails, the
    keywords call still runs, without theme context.
- **Input is the title and description** (plus the theme labels in the keywords call only), in a
  single language chosen from the title (English if present). Keywords are written in that
  language and stored with the title's own language tag; themes are codes and have no language.
- **Proposals live in the database and die with a reload.** The new table references `resource`
  with `ON DELETE CASCADE`, as the `catalogue_db` docstring already requires. Every table must
  mirror the folder at all times, so a proposal can never outlive the metadata it was made from.
  There is no response cache.
- **It is an opt-in pipeline step** (`propose`, last in `STEPS`, not in the default steps), because
  it needs an API key and costs money.

## Consequences

- Every `db` step deletes all proposals; they are regenerated (and paid for again) by the next
  `propose` run. The pipeline logs how many proposals a reload removed. A later decision may add
  a response cache if this proves costly; it would have to be keyed so it cannot go stale.
- The runtime dependency `anthropic` is added; `ANTHROPIC_API_KEY` is read from `.env`.
- "Missing theme" no longer means "no `dcat:theme` row": a resource whose themes are all outside
  the vocabulary still gets in-vocabulary proposals, which is stricter than the MQA metric
  (`categoryAvailability` only checks that the property is set).
- Language tags: stored keywords use the same ISO 639-3 style tag as the data (`eng`); `en` and
  `eng` are both read as English. If proposals are ever exported to RDF/DCAT-AP, converting to
  2-letter tags happens at export, in one place.
