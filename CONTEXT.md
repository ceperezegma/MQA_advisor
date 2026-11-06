# MQA Advisor

This app recommends how to improve the metadata quality of a data.europa.eu catalogue. It reads the catalogue's resources from the SQLite database (built by `pipeline.py`) and, using the MQA Properties, finds what is missing or wrong. The terms below are shared with the MQA_test project this was split from.

## Language

### DCAT-AP Resources

**Dataset**:
A described collection of data sources published under DCAT-AP, evaluated as its own scored entity and as the parent of Distributions and embedded Data Services.

**Distribution**:
One accessible representation of a Dataset (files containing data or specific URL), evaluated as its own scored entity.

**Embedded Data Service**:
A Data Service reached via a Distribution's `dcat:accessService`. Rolled into its parent Dataset's Dataset Final Score, never scored as a Catalogue member in its own right.
_Avoid_: dataset-level data service (a Data Service is never a direct child of a Dataset — only of a Distribution)

**Standalone Data Service**:
A Data Service that sits directly under a Catalogue, with no parent Dataset. Scored independently and included in Catalogue Score alongside Dataset Final Scores.

**Catalogue**:
A collection of Datasets and Standalone Data Services published by one data provider.

**Deprecated Distribution**:
A Distribution whose: 
* `adms:status` = `<http://purl.org/adms/status/Deprecated>`, or 
* `adms:status` = `<http://publications.europa.eu/resource/authority/dataset-status/DEPRECATED>`
is marked as deprecated. Excluded entirely: not evaluated, not counted in its parent Dataset's distribution average, not counted in Coverage. A Dataset whose distributions are *all* deprecated is dropped from Catalogue aggregation entirely — distinct from a Dataset with zero distributions, which still scores via its own Entity Score alone.

### MQA Properties

**Property**:
The smallest unit this methodology scores: one check against one DCAT-AP field on one resource (e.g. "is `dct:title` set"). Binary result — 1 if fulfilled, 0 if not.
_Avoid_: metric, indicator (both appear in source material for the same concept; this project standardizes on Property)

**Importance**:
The tier a Property belongs to — Mandatory, Recommended, or Optional — which determines its Weight.
_Avoid_: Relevance, Layer (synonyms used inconsistently across the source spreadsheet and methodology doc)

**Weight**:
The numeric value attached to a Property's Importance: 1.0 (Mandatory), 0.5 (Recommended), 0.25 (Optional).
