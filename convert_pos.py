# convert_pos.py
#!/usr/bin/env python3
"""
Convert Brigade_Bangalore POS CSV to the standard pos_transactions.csv format.
Run once before starting the pipeline.
"""
import pandas as pd
import sys
from pathlib import Path

def convert(input_path: str, output_path: str = "pos_transactions.csv"):
    df = pd.read_csv(input_path)

    # Aggregate to order level (multiple line items per order)
    orders = (
        df.groupby("order_id")
        .agg(
            order_time=("order_time", "first"),
            order_date=("order_date", "first"),
            total_amount=("total_amount", "sum"),
            customer_number=("customer_number", "first"),
        )
        .reset_index()
    )

    # Build ISO-8601 timestamp
    orders["timestamp"] = pd.to_datetime(
        orders["order_date"].astype(str) + " " + orders["order_time"].astype(str),
        dayfirst=True,
    ).dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    result = pd.DataFrame({
        "store_id": "STORE_BLR_002",
        "transaction_id": "TXN_" + orders["order_id"].astype(str),
        "timestamp": orders["timestamp"],
        "basket_value_inr": orders["total_amount"].round(2),
    })

    result.to_csv(output_path, index=False)
    print(f"Wrote {len(result)} transactions to {output_path}")
    return result

if __name__ == "__main__":
    inp = sys.argv[1] if len(sys.argv) > 1 else "Brigade_Bangalore_10_April_26__1_bc6219c.csv"
    convert(inp)