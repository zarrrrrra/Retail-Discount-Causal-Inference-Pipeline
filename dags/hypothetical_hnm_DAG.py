from datetime import datetime, timedelta

from airflow import DAG
from airflow.providers.databricks.operators.databricks import DatabricksNotebookOperator
from airflow.providers.databricks.operators.databricks_workflow import DatabricksWorkflowTaskGroup

DATABRICKS_CONN_ID = "databricks_default"
NOTEBOOK_BASE = "/Workspace/Users/email@gmail.com/Weekly_Discount_Pipeline"

# Notebook names (change these if you import the simplified notebooks under other names)
NOTEBOOK_INGEST = "02_WeeklyAgregations_Bronze"
NOTEBOOK_EMBEDDINGS = "03_Embeddings_Substitutes"
NOTEBOOK_INCREMENTAL = "04_IncrementalDemand_Silver"
NOTEBOOK_CANNIBALIZATION = "05_Cannibalization_Silver"
NOTEBOOK_GOLD = "06_Tables_Gold"

# Bronze tables (written by notebook 02, read by 03, 04 and 06)
TRANSACTIONS_TABLE = "workspace.default.hnm_transactions_bronze"
ARTICLES_TABLE = "workspace.default.hnm_articles_bronze"

# One schema per layer. Each notebook has its own output_schema, so it is set per task below.
EMBEDDINGS_SCHEMA = "workspace.hnm_discount"          # written by 03
SILVER_SCHEMA = "workspace.hnm_discount_silver"       # written by 04 and 05
GOLD_SCHEMA = "workspace.hnm_discount_gold"           # written by 06 (the dashboard reads this one)

# One shared cluster for all tasks
JOB_CLUSTER_KEY = "hnm_job_cluster"
JOB_CLUSTERS = [
    {
        "job_cluster_key": JOB_CLUSTER_KEY,
        "new_cluster": {
            "spark_version": "15.4.x-scala2.12",
            "node_type_id": "r3.xlarge",
            "num_workers": 1,
            "data_security_mode": "SINGLE_USER",
        },
    }
]

default_args = {
    "owner": "data-eng",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="hnm_discount_pipeline",
    schedule="0 6 * * MON",       # weekly, matching the weekly CSV upload cadence
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    max_active_runs=1,
    tags=["hnm", "causal", "discount-pipeline"],
) as dag:

    with DatabricksWorkflowTaskGroup(
        group_id="hnm_discount_workflow",
        databricks_conn_id=DATABRICKS_CONN_ID,
        job_clusters=JOB_CLUSTERS,
    ) as workflow:

        def notebook_task(task_id, notebook_name, params=None):
            return DatabricksNotebookOperator(
                task_id=task_id,
                databricks_conn_id=DATABRICKS_CONN_ID,
                notebook_path=f"{NOTEBOOK_BASE}/{notebook_name}",
                source="WORKSPACE",
                job_cluster_key=JOB_CLUSTER_KEY,
                notebook_params=params or {},
            )

        # 02: loads the weekly CSVs into the bronze tables
        ingest = notebook_task("ingest_weekly_transactions", NOTEBOOK_INGEST)

        # 03: text + behavior embeddings and the top-k similar articles
        embeddings = notebook_task(
            "build_embeddings", NOTEBOOK_EMBEDDINGS,
            params={
                "transactions_table": TRANSACTIONS_TABLE,
                "articles_table": ARTICLES_TABLE,
                "output_schema": EMBEDDINGS_SCHEMA,
                "top_k_neighbors": "30",
                "svd_components": "64",
                "text_weight": "0.5",
            },
        )

        # 04: incremental demand (discounted vs matched controls) and the exposed-article list
        incremental_demand = notebook_task(
            "incremental_demand", NOTEBOOK_INCREMENTAL,
            params={
                "transactions_table": TRANSACTIONS_TABLE,
                "articles_table": ARTICLES_TABLE,
                "embeddings_schema": EMBEDDINGS_SCHEMA,
                "output_schema": SILVER_SCHEMA,
                "discount_threshold": "0.05",
                "n_controls_per_treated": "5",
                "exposure_top_n": "3",
            },
        )

        # 05: cannibalization (reads the weeks, matching settings and exposed articles saved by 04)
        cannibalization = notebook_task(
            "cannibalization", NOTEBOOK_CANNIBALIZATION,
            params={
                "embeddings_schema": EMBEDDINGS_SCHEMA,
                "output_schema": SILVER_SCHEMA,
            },
        )

        # 06: gold tables for the dashboard
        gold_tables = notebook_task(
            "build_gold_tables", NOTEBOOK_GOLD,
            params={
                "articles_table": ARTICLES_TABLE,
                "embeddings_schema": EMBEDDINGS_SCHEMA,
                "silver_schema": SILVER_SCHEMA,
                "output_schema": GOLD_SCHEMA,
                "high_cannibalization_cutoff": "0.5",
            },
        )

        ingest >> embeddings >> incremental_demand >> cannibalization >> gold_tables
