# Methods

Companion to the [README](README.md). This document covers how each notebook works, the causal design, the tables the pipeline writes and the known limitations.

**Contents:** [Data](#data) · [Similar articles](#similar-articles-notebook-03) · [Causal design](#causal-design-notebooks-04-and-05) · [Headline KPIs](#headline-kpis-notebook-06) · [Dashboard tables](#dashboard-tables) · [Pipeline health](#pipeline-health) · [Pipeline layout and settings](#pipeline-layout-and-settings) · [Known simplifications](#known-simplifications)



# Data
## H&M Personalized Fashion Recommendations
Roughly 31M transactions over two years, 1.37M customers, 105K articles, with a transaction-level price, a sales channel flag and article text descriptions.

The original dataset is segmented to demonstrate the pipeline's orchestration and to mimic how a real business could use this framework with their own weekly sales figures. The original HuggingFace dataset is segmented (by Weeks_Segmented.ipynb) into:
- hnm_transactions.csv: Master transaction dataset with 30,597,413 transactions, from 2018-09-20 (Thursday) to 2020-08-21 (Friday)
- 4 weekly files (week_#_dates.csv): 2020-08-24 to 2020-09-20

Saturday 2020-08-22 and Sunday 2020-08-23 fall between the master table and the first weekly file, so they are in neither (see [Known simplifications](#known-simplifications)).

The demonstrated results and analysis come from two runs, each appending one weekly file to the master table:
- week_35_2020-08-24_to_2020-08-30.csv (treatment week of the first run; its pre-period weeks start 2020-07-27, 2020-08-03, 2020-08-10 and 2020-08-17)
- week_36_2020-08-31_to_2020-09-06.csv (treatment week of the second run; its pre-period weeks start 2020-08-03, 2020-08-10, 2020-08-17 and 2020-08-24)

The following weeks are provided for demonstration:
- week_37_2020-09-07_to_2020-09-13.csv
- week_38_2020-09-14_to_2020-09-20.csv

## Input tables

| Table | Source | Notes |
|---|---|---|
| {articles_table} | (default: workspace.default.hnm_articles_bronze) | HuggingFace H&M articles. Attributes used to build the text blob and the categories |
| {transactions_table} | (default: workspace.default.hnm_transactions_bronze) | Output of notebook 02: Used for the customer × article matrix (before the treatment week) and for the weekly units and prices |



# Similar Articles (notebook 03)
Similar articles for comparison are identified by combining article embeddings built from product attributes with historical customer behaviour patterns.

Notebook 03_Embeddings_Substitute is outcome-blind so it never sees discount status or treatment week sales. It uses only article attributes & pre treatment transactions & publishes a ranked neighbor list for every article.

### Text similarity
Each article's categorical attributes (product name, product type, product group, graphical appearance, colour group, index, section, garment group) are concatenated into a text blob and encoded with a sentence-transformer (all-MiniLM-L6-v2, 384-d, L2-normalised so cosine = dot product,  -1,1 scale). The encoded tensor is cached in a Volume (as embedding_cache) and reused when the number of articles is unchanged; otherwise it is re-encoded.

### Customer behavior
Customer × article purchase counts are factorised with truncated SVD (default 64) to capture similarities in how customers interact with products. Only transactions before the treatment week are used: the cutoff is the Monday of the latest week in the transactions table (the same rule notebook 04 ). Substitute selection therefore cannot depend on post-treatment behaviour.

### Combining the signals
The anchor article is the article you're finding neighbours for, the starting point of a similarity search. The articles found for it are its candidates, and once chosen, its substitutes.
* Similarity is exact cosine, computed in batches of 1,000 anchors (there is no approximate nearest neighbor index).
* An anchor with a behavior embedding scores candidates as `final_score = text_weight * text_sim + (1 - text_weight) * behavior_sim` (default weight 0.5).
* An anchor with no behavior embedding (e.g. an article first sold in the treatment week) scores candidates on text similarity only. `score_basis` (`blend` / `text_only`) records which case applied, because scores are comparable within an anchor but not across the two bases.
* Candidates must have a behavior embedding, i.e. they sold before the cutoff. An article with no pre-period sales has no pre-period outcome, so it cannot serve as a substitute or control and excluding it keeps every candidate on the same scale within an anchor.
* An article is never its own neighbor.
* The top `top_k_neighbors` (default 30) candidates per anchor are kept with `rank_overall`. The list is deliberately deep: notebook 04 drops discounted and ineligible candidates first and then takes the top `exposure_top_n`, instead of losing substitute.

### Outputs (`workspace.hnm_discount`)
| Table | Contents |
|---|---|
| `hnm_article_text_embeddings` | article_id, text_embedding (used by 04 and 05 for matching) |
| `hnm_article_text_nn_candidates` | anchor_id, candidate_id, text_sim, behavior_sim (null for text-only anchors), final_score, rank_overall, score_basis (used by 04; 06 counts its rows for pipeline health) |
| `hnm_embedding_run_meta` | one row, overwritten each run: run_ts, behavior cutoff date, article counts, top_k, text weight, SVD components, SVD explained variance (06 records the run timestamp and the embedding age in `gold_pipeline_health`) |

The run fails (rather than publishing a bad list) if any data quality check fails: no self-matches, no duplicate anchor/candidate pairs, every candidate has a behavior embedding and every anchor has a neighbor list; the checks run before the tables are saved.



# Causal Design (notebooks 04 and 05)

**Unit of analysis:** article × week. For the DiD, the 4 pre period weeks are averaged, so each article contributes one pre-period  & 1 post-period observation.

**Outcome:** `Y = units sold` (transaction rows)

**Time periods:** the 5 most recent weeks that have transactions, with weeks starting on Monday.
- `pre`  → mean of weeks 1–4 (baseline)
- `post` → week 5 (discount week)

**Treatment**
* `T = 1` → the article's average week-5 price is at least `discount_threshold` (default 5%) below its transaction-weighted pre-period price
* `T = 0` → the article was not discounted (it is either a clean control or an exposed substitute, see below)

**Eligibility:** an article is analysed only if it averaged at least 1 unit per week in the pre-period and sold at least once in week 5.

**Groups**
* **Treated:** discounted in week 5.
* **Exposed substitutes:** the top `exposure_top_n` (default 3) eligible, non-discounted articles in a discounted article's neighbor list, ranked by `final_score`. Discounted and ineligible candidates are dropped before ranking, so a discounted article can have fewer than 3 exposed substitutes if its 30-neighbor list runs out. This group is expected to lose sales to cannibalization and is therefore not a valid counterfactual for the treated group.
* **Clean control:** an eligible, non-discounted article that is not exposed and has a text embedding. It is the counterfactual for *both* estimates below — it reflects what happened that week for articles untouched by any discount, holding constant seasonality, marketing and other week-5-specific effects that a simple before/after comparison on the treated article's own history would otherwise misattribute to the discount.

Notebook 04 builds the exposed set once and saves it (`hnm_exposure_pairs`). Notebook 05 reads that same table, so the incremental-demand and cannibalization estimates use the same exposed set and the same clean-control pool (each estimate matches its own controls from that pool).

## Matching
Each treated article (and, in the cannibalization estimate, each exposed article) is matched to its 5 best clean controls, with replacement. The match score is the text-embedding cosine similarity minus two penalties:

`score = cosine − 0.10 × |difference in log pre-period level| − 0.30 × |difference in pre-period log trend|`

Level is log(1 + average pre-period weekly units); trend is the slope of log(1 + weekly units) across the four pre-period weeks.

A control used `m` times gets weight `m / 5`, so the controls together weigh as much as the articles they were matched to. Treated articles with no embedding are left out.

## 1. Article-Level Incremental Demand (notebook 04)
Did discounted articles experience an increase in sales relative to clean control as DiD?
$$ATT_{demand} =
(Y_{treated,post} - Y_{treated,pre}) -(Y_{control,post} - Y_{control,pre})$$

Where `control` = matched clean control (not the substitute group). Estimated as a weighted 2×2 regression (`y ~ T * post`) with standard errors clustered by article. The coefficient on `T:post` is the estimate and notebook 04 prints the full regression table.

## 2. Substitute-Level Cannibalization (notebook 05)
Did similar, non-discounted articles lose sales after the anchor article was discounted, relative to clean control?
$$ATT_{sub} =(Y_{sub,post} - Y_{sub,pre}) -(Y_{control,post} - Y_{control,pre})$$
$$Cannibalized\ Units = -\,ATT_{sub} \times \text{Number of Distinct Exposed Articles}$$
This is the same as minus the sum of the adjusted change of every exposed article. An article exposed to several discounted articles is counted once. In the pair table (`hnm_cannibalization_pair_effects`) its change is split equally across its discounted anchors, so the pair table adds up to the total.

A negative `ATT_sub` (ie, positive cannibalized units) indicates the substitute group lost sales relative to clean control after the anchor article was discounted.

## 3. Net Category-Level Effect (notebook 06)
$$Net\ Effect = Gross\ Incremental\ Units - Cannibalized\ Units$$
A positive net effect indicates estimated incremental sales exceeded estimated losses among substitute articles. A negative net effect means fewer total units across the discounted and exposed articles than the controls imply: either losses among substitutes outweighed a positive gross effect, or the gross effect was itself negative (flagged as `NO_POSITIVE_GROSS_EFFECT`).

## Parallel-trends check
Before the discount, the treated group and its matched controls should move together. Each week, both notebooks take the difference between the groups' weighted average log(1 + units) and subtract that difference's pre-period average. `pre_gap_max` is the largest absolute value of this in a pre-period week. Above 0.15 the notebooks print a warning and notebook 06 sets `PRE_TREND_WARNING` in `kpi_status`. In that case the estimate is descriptive only. The weekly average units (in levels) are saved for the dashboard chart (`parallel_trends_demand`, `parallel_trends_cannibalization`).



# Headline KPIs (notebook 06)

### Gross Incremental Units
Estimated additional units associated with discounting:
$$Gross = ATT_{demand} \times Number\ of\ Matched\ Discounted\ Articles$$

### Cannibalized Units
Estimated lost units among similar non-discounted articles (each exposed article counted once).

### Net Incremental Units Aggregate
$$Net = Gross - Cannibalized$$

### % of Lift Cannibalized
$$\%Cannibalized =\frac{Cannibalized}{Gross}$$
This metric is shown only when estimated gross incremental demand is greater than zero.

### Statistical Uncertainty
**95% confidence intervals** for both DiD estimates and for net units (the net interval assumes the two estimates are independent; see [Known simplifications](#known-simplifications)).

### Status
`kpi_status` is OK, or a list of NO_POSITIVE_GROSS_EFFECT (gross is zero or negative, so the cannibalized share is blank) and PRE_TREND_WARNING (see the parallel-trends check).

### Category rollup
Category metrics are aggregated by product group and garment group , the rollup adds up to the headline numbers and the notebook stops if it does not.

In `gold_category_rollup`, a category is flagged when more than 50% of its estimated incremental lift is cannibalized (`high_cannibalization_cutoff`, default 0.5). The flag is only set for categories with positive gross lift.



# Dashboard Tables
The dashboard reads only the gold tables in `workspace.hnm_discount_gold` (written by notebook 06):

| Table | Contents |
|---|---|
| `gold_headline_kpis` | one row: gross, cannibalized and net units, confidence intervals, % cannibalized, `kpi_status` |
| `gold_article_incremental_demand` | one row per matched discounted article: price, discount %, baseline and week-5 units, `lift_units`, `control_delta`, `adj_lift_units`, categories |
| `gold_cannibalization_leakage` | one row per discounted article and exposed article, with product names, score and unit change |
| `gold_category_rollup` | gross, cannibalized and net units per product group and garment group, with a high-cannibalization flag |
| `gold_parallel_trends_demand`, `gold_parallel_trends_cannibalization` | weekly averages for the pre-trend charts |
| `gold_pipeline_health` | one row per run (history is kept) |

## Article Drill-Down
For each discounted article, the dashboard shows:
```text
lift units = Week 5 units − 4-week average units
```
 `lift_units` is a descriptive before/after comparison and is **not a causal estimate on its own**.
The causal estimate comes from the DiD analysis and the per article version is `adj_lift_units` (lift_units -  average change of the article's matched controls = control_delta). The average of `adj_lift_units` equals the DiD estimate and notebook 04 checks this.


# Pipeline Health

Each pipeline run appends one row to `gold_pipeline_health` with basic production metrics:

* Articles analysed, discounted articles and non-discounted articles
* Number of similar-article (neighbor) rows
* Incremental-demand sample sizes (treated, matched controls, control pool) and R²
* Number of exposed substitutes
* Embedding run timestamp and its age in hours
* Largest pre-trend gap for each estimate
* `kpi_status`

Data-quality checks make it so if one fails, that task fails and the later tasks do not run, so a bad result is not published.


# Pipeline Layout and Settings

Each layer of the pipeline writes to its own schema. Notebooks 03–06 take their output schema as a widget (`output_schema`) and the notebooks that read an earlier layer take that layer's schema as a widget too (`embeddings_schema`, `silver_schema`).

| Notebook | Reads | Writes | Schema |
|---|---|---|---|
| 01 Uploads|  HuggingFace API | `hnm_articles_bronze` & `hnm_transactions_bronze` (master transactions table) | `workspace.default` |
| 02 Ingestion | Newest weekly CSV in `/Volumes/workspace/default/hnm_weekly_upload/` | Appends to `hnm_transactions_bronze`, only if the file's latest date is newer than the table's (the article table `hnm_articles_bronze` comes from the HuggingFace dataset) | `workspace.default` |
| 03 Embeddings | Bronze tables | `hnm_article_text_embeddings`, `hnm_article_text_nn_candidates`, `hnm_embedding_run_meta` | `workspace.hnm_discount` |
| 04 Incremental demand | Bronze tables, 03 outputs | `hnm_article_treatment_weekly`, `hnm_units_long`, `hnm_exposure_pairs`, `hnm_incremental_demand_did`, `parallel_trends_demand` | `workspace.hnm_discount_silver` |
| 05 Cannibalization | 04 outputs, 03 embeddings | `hnm_cannibalization_pair_effects`, `hnm_cannibalization_did`, `parallel_trends_cannibalization` | `workspace.hnm_discount_silver` |
| 06 Gold tables | 04 and 05 outputs, bronze articles, 03 run metadata and neighbor table | `gold_*` tables ([Dashboard Tables](#dashboard-tables)) | `workspace.hnm_discount_gold` |

**Settings (widgets, with defaults)**
* **03:** `transactions_table`, `articles_table`, `output_schema`, `top_k_neighbors` (30), `svd_components` (64), `text_weight` (0.5)
* **04:** `transactions_table`, `articles_table`, `embeddings_schema`, `output_schema`, `discount_threshold` (0.05), `n_controls_per_treated` (5), `exposure_top_n` (3). Fixed in code: 5 weeks, minimum 1 unit per pre-period week, matching penalties 0.10 (level) and 0.30 (trend).
* **05:** `embeddings_schema`, `output_schema`. The treatment week, pre-period weeks and matching settings are read from notebook 04's result table, so the two notebooks always agree.
* **06:** `articles_table`, `embeddings_schema`, `silver_schema`, `output_schema`, `high_cannibalization_cutoff` (0.5). Fixed in code: pre-trend warning level 0.15.

In the hypothetical Airflow DAG, notebooks 03–06 each receive their own parameters, because they read from and write to different schemas.



# Known Simplifications

- **Articles that are not analysed are dropped, not treated as controls.** An article must average at least 1 unit per week in the pre-period and sell at least once in week 5, discontinued or stocked-out articles are therefore excluded. If a discount is what keeps an article selling, or if discounted articles are being cleared out, the analysed group is not a random sample of all discounts.

- **Discounted articles may already be in decline.** Articles are often discounted because they are selling poorly. If no matched control shares that decline, the pre-trend check fails and the estimate (which can even have the wrong sign) should be read as descriptive. 
- **Matching uses only text similarity and pre-period sales level and trend.** Price, availability and sales channel are not matched on.
- **The discount is measured on the average price.** The week-5 price and the pre-period price are averaged over all sales, so a change in sales-channel mix can look like a discount.
- **Exposure is defined by similarity, not by observed switching.** The top `exposure_top_n` similar non-discounted articles are assumed to be the ones affected. Articles outside that list could also lose sales and they are used as controls.

- **Cannibalization is split equally across anchors.** An exposed article that is similar to several discounted articles has its change divided equally between them in the pair table, the total is not affected.
- **Net-units confidence interval assumes independence.** The incremental and cannibalization DiD estimates are combined assuming independent standard errors. In practice the two analyses share the same control pool, so this is a simplifying approximation rather than a guarantee.

- **Single post-period.** Treatment effects are estimated from one post-period (week 5) against a 4-week pre-period average. A single post-week is more exposed to week-specific noise (e.g., holidays, weather, stockouts) than a design with multiple post-periods, since only the pre-period is averaged.
- **Text blobs are attribute-only and tie heavily.** Many articles share an identical text blob (same categorical attributes), so text similarity alone cannot distinguish them; if a behavior signal exists it breaks the tie.
- **Text-only anchors are a different scale.** Articles with no pre-period sales (new launches) get neighbors ranked on text similarity only, so their `final_score` is not comparable to blended scores from other anchors. This does not affect the current estimates: every analysed article sold in the pre-period, so every discounted anchor in notebook 04 has a blended neighbor list.

- **Articles without pre-period sales are never candidates.** An article first sold in the treatment week cannot appear as someone else's substitute, even if it is genuinely similar. Such articles are also ineligible for notebook 04 (no pre-period baseline), so this does not change the current estimates.
- **Behavior SVD uses raw purchase counts.** The leading component is likely to reflect popularity, which can make popular articles look similar to each other.

- **The text-embedding cache checks only the article count.** If article attributes change without the number of articles changing, delete the cached file in the `embedding_cache` Volume so the text is re-encoded.
- **Every article is compared with every other article.** This finds the true closest matches, but the work grows quickly: twice as many articles means about four times the work. It runs fine for about 105K articles, but a much larger catalogue would need a faster search that finds close, but not always exact, matches.


