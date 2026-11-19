# What is in this repository

This repository belongs to the study "Nowhere to Report: Vulnerability Disclosure Channels at US Hospitals, Medical Device Makers, and Health IT Developers. A Cross-sectional Web Measurement" (Christopher Jagdeo, Independent Researcher).

`README.md`, the page shown first, is the information page that the crawler's user agent pointed to. It is kept unchanged. This file describes everything else. The files are a snapshot taken on November 19, 2026, after both data-collection passes (November 17 and November 19, 2026) and the final analysis.

## Contents

- `protocol/PROTOCOL.md`: the frozen protocol (SHA-256 `469d0582a1cbea05d4b8fc5b63c26a6e554c8748b641f688716e501e79af0a51`). `protocol/FROZEN.txt` names the commit of the study's development history at which it was frozen.
- `src/noroshi/`: the crawler, the classifiers and the code that derives the outcomes and builds the tables. `pyproject.toml`, `uv.lock` and `.python-version` pin the environment.
- `tests/`: the test suite.
- `info/`: the information page as the crawler knew it, and the opt-out list (empty: no opt-out request was received).
- `data/frames/`: the five lists the study started from (hospitals, federal control, device makers, advisory vendors, EHR developers), the source tables they were built from, and the records of how company names were matched to domains.
- `data/reference/`: the rural-urban codes used. `registries/registries.meta.json` pins the outside directories by commit and hash; the copies themselves are not redistributed here.
- `data/derived/domains.csv`: one row per list entry, with the domain's status and outcomes. `data/derived/hospitals.csv`: one row per hospital. The two `wayback_*.csv` files list the Internet Archive lookups that were asked for.
- `results/`: the tables, figures and `numbers.json` written by `noroshi analyze`.
- `analysis/`: the scripts and outputs of the analyses made outside `noroshi analyze` (the post hoc concentration analysis, hospitals kept against hospitals dropped, hospitals on measured against unmeasured domains, the first-seen summary, Figure 1), `audit_numbers.json` (the numbers the paper takes from the final audit), and `independent/` (the code and report of the independent recomputation).
- `logs/ai-use-log.md`: the running log of AI use.
- `STROBE-checklist.md`: the reporting checklist.

## What is withheld

- The stored copies of web pages and files (the raw store) and the model-call log, which holds page text.
- Contact details found in security.txt files.
- 21 test fixtures that are copies of third-party web pages; the tests that read them (in `tests/test_discover.py` and `tests/test_crawl.py`) fail without them.
- The audit reports, which discuss individual sites.
- Copies of third-party directories, which are pinned by commit and hash instead.

## Notes for readers

- A result of "no channel found" for a domain means only that this study's method found none on the dates given. It is not a statement about that organization's security. A status of unreachable, blocked or mismatch means the domain could not be measured, not that it lacks a channel.
- Commit identifiers quoted in the paper and its supplement refer to the study's development history, which is not part of this snapshot. `protocol/FROZEN.txt` therefore names a commit that this repository does not contain, and the crawl command, which checks it, will not run from here. The snapshot documents the study; it is not set up to crawl these sites again.
- The scripts in `analysis/` were run from a folder beside the repository (`../noroshi-paper/analysis/`) and are included as they were run, with their outputs. `posthoc_concentration.py` and `first_seen_summary.py` also read the raw store.
