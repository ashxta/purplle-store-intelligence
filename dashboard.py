# dashboard.py
"""
dashboard.py — Live terminal dashboard for store metrics.
Updates every 5 seconds. Shows key metrics, funnel, and active anomalies.

Run: python dashboard.py [--api-url http://localhost:8000] [--store-id STORE_BLR_002]
"""
import argparse
import os
import time
from datetime import datetime

import requests
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

API_URL = os.getenv("API_URL", "http://localhost:8000")
STORE_ID = os.getenv("STORE_ID", "STORE_BLR_002")
REFRESH_SEC = 5


def fetch(path: str) -> dict | None:
    try:
        r = requests.get(f"{API_URL}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return None


def make_metrics_panel(data: dict | None) -> Panel:
    if not data:
        return Panel("[red]API unavailable[/]", title="Metrics")

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", width=28)
    t.add_column(style="bold white")

    visitors = data.get("unique_visitors", 0)
    conv = data.get("conversion_rate", 0)
    queue = data.get("current_queue_depth", 0)
    abandon = data.get("abandonment_rate", 0)
    revenue = data.get("total_revenue_inr", 0)
    transactions = data.get("total_transactions", 0)

    conv_color = "green" if conv >= 0.15 else "yellow" if conv >= 0.08 else "red"
    queue_color = "red" if queue >= 10 else "yellow" if queue >= 5 else "green"

    t.add_row("Unique visitors today", f"[bold]{visitors}[/]")
    t.add_row("Conversion rate", f"[{conv_color}]{conv:.1%}[/]")
    t.add_row("Current queue depth", f"[{queue_color}]{queue}[/]")
    t.add_row("Abandonment rate", f"{abandon:.1%}")
    t.add_row("Total revenue (₹)", f"₹{revenue:,.0f}")
    t.add_row("Transactions", f"{transactions}")
    t.add_row("Last updated", data.get("as_of", "-"))

    return Panel(t, title=f"[bold cyan]📊 {data.get('store_id', STORE_ID)} — Metrics[/]")


def make_funnel_panel(data: dict | None) -> Panel:
    if not data:
        return Panel("[red]unavailable[/]", title="Funnel")

    t = Table(show_header=True, header_style="bold")
    t.add_column("Stage", style="white", width=18)
    t.add_column("Sessions", justify="right", width=10)
    t.add_column("Drop-off", justify="right", width=10)
    t.add_column("Bar", width=20)

    stages = data.get("stages", [])
    max_count = max((s["count"] for s in stages), default=1) or 1

    for stage in stages:
        count = stage["count"]
        drop = stage["drop_off_pct"]
        bar_len = int(count / max_count * 18)
        bar = "█" * bar_len
        drop_color = "red" if drop > 50 else "yellow" if drop > 25 else "green"
        t.add_row(
            stage["stage"],
            str(count),
            f"[{drop_color}]{drop:.1f}%[/]" if drop > 0 else "-",
            f"[cyan]{bar}[/]",
        )

    return Panel(t, title="[bold cyan]🔽 Conversion Funnel[/]")


def make_heatmap_panel(data: dict | None) -> Panel:
    if not data:
        return Panel("[red]unavailable[/]", title="Zone Heatmap")

    t = Table(show_header=True, header_style="bold")
    t.add_column("Zone", style="white", width=16)
    t.add_column("Visits", justify="right", width=8)
    t.add_column("Avg dwell", justify="right", width=12)
    t.add_column("Heat", width=12)

    for zone in sorted(data.get("zones", []), key=lambda z: -z["visit_frequency_normalised"]):
        freq = zone["visit_frequency_normalised"]
        dwell_s = zone["avg_dwell_ms"] / 1000
        bar_len = int(freq / 100 * 10)
        color = "red" if freq > 75 else "yellow" if freq > 40 else "green"
        t.add_row(
            zone["zone_id"],
            str(zone["visit_count"]),
            f"{dwell_s:.0f}s",
            f"[{color}]{'█' * bar_len}[/]",
        )

    confidence = data.get("data_confidence", "?")
    conf_color = "yellow" if confidence == "low" else "green"
    footer = f"Confidence: [{conf_color}]{confidence}[/]"
    return Panel(t, title="[bold cyan]🗺  Zone Heatmap[/]", subtitle=footer)


def make_anomalies_panel(data: dict | None) -> Panel:
    if not data:
        return Panel("[red]unavailable[/]", title="Anomalies")

    anomalies = data.get("anomalies", [])
    if not anomalies:
        return Panel("[green]✓ No active anomalies[/]", title="[bold cyan]⚠  Anomalies[/]")

    t = Table(show_header=True, header_style="bold")
    t.add_column("Type", width=24)
    t.add_column("Severity", width=10)
    t.add_column("Description", width=40)

    sev_colors = {"CRITICAL": "red", "WARN": "yellow", "INFO": "blue"}
    for a in anomalies:
        sev = a["severity"]
        color = sev_colors.get(sev, "white")
        t.add_row(
            a["anomaly_type"],
            f"[{color}]{sev}[/]",
            a["description"][:60],
        )

    return Panel(t, title="[bold cyan]⚠  Active Anomalies[/]")


def make_header() -> Text:
    now = datetime.now().strftime("%H:%M:%S")
    return Text(
        f"  Purplle Store Intelligence  │  {STORE_ID}  │  {now}  │  Refreshing every {REFRESH_SEC}s",
        style="bold on blue",
        justify="center",
    )


def render() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="top", size=16),
        Layout(name="bottom"),
    )
    layout["top"].split_row(
        Layout(name="metrics"),
        Layout(name="funnel"),
    )
    layout["bottom"].split_row(
        Layout(name="heatmap"),
        Layout(name="anomalies"),
    )

    metrics_data = fetch(f"/stores/{STORE_ID}/metrics")
    funnel_data = fetch(f"/stores/{STORE_ID}/funnel")
    heatmap_data = fetch(f"/stores/{STORE_ID}/heatmap")
    anomaly_data = fetch(f"/stores/{STORE_ID}/anomalies")

    layout["header"].update(make_header())
    layout["metrics"].update(make_metrics_panel(metrics_data))
    layout["funnel"].update(make_funnel_panel(funnel_data))
    layout["heatmap"].update(make_heatmap_panel(heatmap_data))
    layout["anomalies"].update(make_anomalies_panel(anomaly_data))

    return layout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=API_URL)
    parser.add_argument("--store-id", default=STORE_ID)
    args = parser.parse_args()

    global API_URL, STORE_ID
    API_URL = args.api_url
    STORE_ID = args.store_id

    console.print(f"[bold cyan]Starting dashboard → {API_URL}[/]")
    with Live(render(), refresh_per_second=1 / REFRESH_SEC, screen=True) as live:
        while True:
            time.sleep(REFRESH_SEC)
            live.update(render())


if __name__ == "__main__":
    main()