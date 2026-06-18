import os
import logging
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery
from dotenv import load_dotenv
from prophet import Prophet
from prophet.diagnostics import cross_validation, performance_metrics

load_dotenv()

PROJECT_ID = "expensetracker-485100"
DATASET_ID = "credit_card_analytics"
GOLD_TABLE_ID = "monthly_category_spend"
FORECAST_TABLE_ID = "spend_forecast"

SERVICE_ACCOUNT_KEY = "/Users/kanwarraj/Documents/Expense_Tracker/Keys/expensetracker-485100-0a36841a58b8.json"
if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SERVICE_ACCOUNT_KEY

MIN_MONTHS = 6
FORECAST_MONTHS = 3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def fetch_monthly_spend(client: bigquery.Client) -> pd.DataFrame:
    query = f"""
        SELECT user_name, category, spend_month, total_amount
        FROM `{PROJECT_ID}.{DATASET_ID}.{GOLD_TABLE_ID}`
        ORDER BY user_name, category, spend_month
    """
    df = client.query(query).to_dataframe(create_bqstorage_client=False)
    df["spend_month"] = pd.to_datetime(df["spend_month"])
    return df


def run_prophet_for_group(df_group: pd.DataFrame, global_max: pd.Timestamp, forecast_months: int = FORECAST_MONTHS) -> tuple[pd.DataFrame | None, dict | None]:
    prophet_df = df_group[["spend_month", "total_amount"]].rename(
        columns={"spend_month": "ds", "total_amount": "y"}
    )

    # --- Data Densification ---
    # Create a continuous timeline from the category's first month up to the global max month
    group_min = prophet_df["ds"].min()
    full_dates = pd.date_range(start=group_min, end=global_max, freq="MS")
    full_df = pd.DataFrame({"ds": full_dates})

    # Merge and fill missing months with 0
    prophet_df = full_df.merge(prophet_df, on="ds", how="left")
    prophet_df["y"] = prophet_df["y"].fillna(0)

    # Safety check: ensure we have enough continuous months after densification
    if len(prophet_df) < MIN_MONTHS:
        return None, None

    model = Prophet(
        yearly_seasonality=True,
        weekly_seasonality=False,
        daily_seasonality=False,
        changepoint_prior_scale=0.3,
        uncertainty_samples=100,
    )
    model.add_country_holidays(country_name='CA')
    model.fit(prophet_df)

    # --- Cross-Validation & MAPE Calculation ---
    metrics_dict = None
    n_months = len(prophet_df)
    if n_months >= 18:
        logger.info(f"Running CV for {n_months} densified months...")
        try:
            cv_df = cross_validation(
                model,
                initial=pd.Timedelta(days=365),
                period=pd.Timedelta(days=30),
                horizon=pd.Timedelta(days=90),
                parallel="processes"
            )
            metrics_df = performance_metrics(cv_df)

            # Try to use MAPE, but fall back to RMSE/MAE if MAPE is unavailable (due to zero values)
            rmse = metrics_df['rmse'].mean()
            mae = metrics_df['mae'].mean() if 'mae' in metrics_df.columns else None

            if 'mape' in metrics_df.columns:
                mape_values = metrics_df['mape'].dropna()
                if len(mape_values) > 0:
                    mape = mape_values.mean()
                    metrics_dict = {
                        'mape': round(mape * 100, 2),
                        'rmse': round(rmse, 2),
                        'mae': round(mae, 2) if mae else None,
                        'n_samples': len(metrics_df)
                    }
                    logger.info(f"✓ CV Metrics - MAPE: {metrics_dict['mape']}%, RMSE: ${metrics_dict['rmse']}")
                else:
                    metrics_dict = {
                        'mape': None,
                        'rmse': round(rmse, 2),
                        'mae': round(mae, 2) if mae else None,
                        'n_samples': len(metrics_df)
                    }
                    logger.info(f"✓ CV Metrics (sparse data) - RMSE: ${metrics_dict['rmse']}, MAE: ${metrics_dict['mae']}")
            else:
                metrics_dict = {
                    'mape': None,
                    'rmse': round(rmse, 2),
                    'mae': round(mae, 2) if mae else None,
                    'n_samples': len(metrics_df)
                }
                logger.info(f"✓ CV Metrics (no MAPE) - RMSE: ${metrics_dict['rmse']}, MAE: ${metrics_dict['mae']}")
        except Exception as e:
            logger.warning(f"Cross-validation exception: {str(e)}")
            metrics_dict = None

    future = model.make_future_dataframe(periods=forecast_months, freq="MS")
    forecast = model.predict(future)

    last_historical = prophet_df["ds"].max()
    future_only = forecast[forecast["ds"] > last_historical][
        ["ds", "yhat", "yhat_lower", "yhat_upper"]
    ].copy()
    future_only["yhat"] = future_only["yhat"].clip(lower=0)
    future_only["yhat_lower"] = future_only["yhat_lower"].clip(lower=0)

    return future_only, metrics_dict


def ensure_forecast_table(client: bigquery.Client):
    table_ref = client.dataset(DATASET_ID).table(FORECAST_TABLE_ID)
    schema = [
        bigquery.SchemaField("user_name", "STRING"),
        bigquery.SchemaField("category", "STRING"),
        bigquery.SchemaField("forecast_month", "DATE"),
        bigquery.SchemaField("predicted_amount", "FLOAT"),
        bigquery.SchemaField("predicted_lower", "FLOAT"),
        bigquery.SchemaField("predicted_upper", "FLOAT"),
        bigquery.SchemaField("forecast_generated_ts", "TIMESTAMP"),
    ]
    try:
        client.get_table(table_ref)
        logger.info(f"Table {FORECAST_TABLE_ID} already exists.")
    except Exception:
        client.create_table(bigquery.Table(table_ref, schema=schema))
        logger.info(f"Created table {FORECAST_TABLE_ID}.")


def write_forecast_to_bq(client: bigquery.Client, df: pd.DataFrame):
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{FORECAST_TABLE_ID}"
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    client.load_table_from_dataframe(df, table_ref, job_config=job_config).result()
    logger.info(f"Wrote {len(df)} forecast rows to {FORECAST_TABLE_ID}.")


def main():
    client = bigquery.Client(project=PROJECT_ID)
    ensure_forecast_table(client)

    logger.info("Fetching monthly_category_spend from BigQuery...")
    df = fetch_monthly_spend(client)
    logger.info(f"Loaded {len(df)} rows across {df['category'].nunique()} categories.")

    generated_ts = datetime.now(timezone.utc)
    all_forecasts = []
    all_metrics = []

    # Determine the absolute latest month in the entire dataset
    global_max = df["spend_month"].max()

    for (user_name, category), group in df.groupby(["user_name", "category"]):
        forecast, metrics = run_prophet_for_group(group.copy(), global_max)
        if forecast is None:
            logger.info(f"Skipping {user_name}/{category} — fewer than {MIN_MONTHS} months.")
            continue

        forecast["user_name"] = user_name
        forecast["category"] = category
        forecast["forecast_generated_ts"] = generated_ts
        forecast = forecast.rename(columns={
            "ds": "forecast_month",
            "yhat": "predicted_amount",
            "yhat_lower": "predicted_lower",
            "yhat_upper": "predicted_upper",
        })
        all_forecasts.append(
            forecast[[
                "user_name", "category", "forecast_month",
                "predicted_amount", "predicted_lower", "predicted_upper",
                "forecast_generated_ts",
            ]]
        )

        # Collect metrics if available
        if metrics:
            metrics["user_name"] = user_name
            metrics["category"] = category
            all_metrics.append(metrics)
            mape_str = f"{metrics['mape']}%" if metrics.get('mape') else "N/A (sparse data)"
            logger.info(
                f"{user_name}/{category}: MAPE={mape_str}, "
                f"RMSE=${metrics['rmse']} | Forecast: {forecast[['forecast_month', 'predicted_amount']].to_dict('records')}"
            )
        else:
            logger.info(
                f"{user_name}/{category} (no CV, insufficient data): Forecast: {forecast[['forecast_month', 'predicted_amount']].to_dict('records')}"
            )

    if not all_forecasts:
        logger.warning("No categories had enough data to forecast.")
        return

    final_df = pd.concat(all_forecasts, ignore_index=True)
    final_df["forecast_month"] = final_df["forecast_month"].dt.date
    write_forecast_to_bq(client, final_df)
    logger.info("Done.")

    # Log metrics summary
    if all_metrics:
        metrics_df = pd.DataFrame(all_metrics)
        logger.info("\n" + "="*80)
        logger.info("CROSS-VALIDATION SUMMARY")
        logger.info("="*80)
        logger.info(f"Total categories evaluated: {len(metrics_df)}")

        # Handle MAPE metrics
        mape_values = metrics_df['mape'].dropna()
        if len(mape_values) > 0:
            logger.info(f"Categories with MAPE: {len(mape_values)}/{len(metrics_df)}")
            logger.info(f"Mean MAPE: {mape_values.mean():.2f}%")
            logger.info(f"Median MAPE: {mape_values.median():.2f}%")
            logger.info(f"Best MAPE: {mape_values.min():.2f}% ({metrics_df.loc[metrics_df['mape'].idxmin(), 'category']})")
            logger.info(f"Worst MAPE: {mape_values.max():.2f}% ({metrics_df.loc[metrics_df['mape'].idxmax(), 'category']})")
        else:
            logger.info("No MAPE values available (sparse data with many zeros)")

        # Always show RMSE
        logger.info(f"\nRMSE Statistics:")
        logger.info(f"Mean RMSE: ${metrics_df['rmse'].mean():.2f}")
        logger.info(f"Median RMSE: ${metrics_df['rmse'].median():.2f}")

        logger.info("="*80 + "\n")

        # Save metrics to CSV for reference
        metrics_csv_path = "/tmp/forecast_metrics.csv"
        metrics_df.to_csv(metrics_csv_path, index=False)
        logger.info(f"Metrics saved to {metrics_csv_path}")


if __name__ == "__main__":
    main()