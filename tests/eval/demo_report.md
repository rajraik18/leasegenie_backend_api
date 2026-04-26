# LeaseGenie Evaluation Report

**Timestamp:** 2026-04-18T12:41:57.254341
**Documents evaluated:** 1
**Total fields scored:** 37

## Headline metrics

- **Exact match rate:** 83.8%
- **Partial match rate:** 100.0%
- **Weighted score:** 95.1%  (EM=1.0, PM=0.7)
- **Expected Calibration Error:** 0.0824  (target: <0.05)
- **Reviewer hours saved:** 0.9

## Per-category accuracy

| Category | N | Exact | Partial | Weighted |
|---|---|---|---|---|
| Basic Information | 13 | 100.0% | 100.0% | 100.0% |
| Critical Clauses | 8 | 87.5% | 100.0% | 96.3% |
| Financial Clauses | 5 | 80.0% | 100.0% | 94.0% |
| Other Lease Clauses | 7 | 71.4% | 100.0% | 91.4% |
| Reimbursements | 4 | 50.0% | 100.0% | 85.0% |

## Per-difficulty accuracy

| Difficulty | N | Exact Match |
|---|---|---|
| 1 | 13 | 100.0% |
| 2 | 14 | 71.4% |
| 3 | 5 | 80.0% |
| 4 | 5 | 80.0% |

## Per-document summary

| Document | Fields | EM | Weighted | Hours Saved |
|---|---|---|---|---|
| Sample 6.pdf | 37 | 83.8% | 95.1% | 0.86 |

## Top 10 failures across corpus

- **Sample 6.pdf** / `estoppel_certificate` (conf=0.93): VALUE_OK_CITATION_WRONG
- **Sample 6.pdf** / `holdover` (conf=0.70): VALUE_OK_CITATION_WRONG