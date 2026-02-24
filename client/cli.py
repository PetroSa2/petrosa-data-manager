"""
Command-line interface for Data Manager Client.
"""

import asyncio
import json
import sys
from datetime import datetime
from typing import Optional

import click
from data_manager_client import DataManagerClient


@click.group()
@click.option(
    "--base-url", default="http://localhost:8000", help="Data Manager API base URL"
)
@click.option("--timeout", default=30, help="Request timeout in seconds")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.pass_context
def cli(ctx, base_url: str, timeout: int, verbose: bool):
    """Petrosa Data Manager Client CLI."""
    ctx.ensure_object(dict)
    ctx.obj["base_url"] = base_url
    ctx.obj["timeout"] = timeout
    ctx.obj["verbose"] = verbose


@cli.command()
@click.argument("database")
@click.argument("collection")
@click.option("--filter", help="JSON filter conditions")
@click.option("--sort", help="JSON sort specification")
@click.option("--limit", default=100, help="Maximum records to return")
@click.option("--offset", default=0, help="Number of records to skip")
@click.option("--fields", help="Comma-separated fields to include")
@click.pass_context
def query(
    ctx,
    database: str,
    collection: str,
    filter: str | None,
    sort: str | None,
    limit: int,
    offset: int,
    fields: str | None,
):
    """Query records from a database collection."""

    async def _query():
        client = DataManagerClient(
            base_url=ctx.obj["base_url"], timeout=ctx.obj["timeout"]
        )

        try:
            filter_dict = json.loads(filter) if filter else None
            sort_dict = json.loads(sort) if sort else None
            field_list = fields.split(",") if fields else None

            result = await client.query(
                database=database,
                collection=collection,
                filter=filter_dict,
                sort=sort_dict,
                limit=limit,
                offset=offset,
                fields=field_list,
            )

            print(json.dumps(result, indent=2, default=str))

        finally:
            await client.close()

    asyncio.run(_query())


@cli.command()
@click.argument("database")
@click.argument("collection")
@click.option("--data", required=True, help="JSON data to insert")
@click.option("--schema", help="Schema name for validation")
@click.option("--validate", is_flag=True, help="Enable schema validation")
@click.pass_context
def insert(
    ctx,
    database: str,
    collection: str,
    data: str,
    schema: str | None,
    validate: bool,
):
    """Insert records into a database collection."""

    async def _insert():
        client = DataManagerClient(
            base_url=ctx.obj["base_url"], timeout=ctx.obj["timeout"]
        )

        try:
            data_dict = json.loads(data)
            result = await client.insert(
                database=database,
                collection=collection,
                data=data_dict,
                schema=schema,
                validate=validate,
            )

            print(json.dumps(result, indent=2, default=str))

        finally:
            await client.close()

    asyncio.run(_insert())


@cli.command()
@click.argument("pair")
@click.argument("period")
@click.option("--start", help="Start timestamp (ISO format)")
@click.option("--end", help="End timestamp (ISO format)")
@click.option("--limit", default=100, help="Maximum candles to return")
@click.option("--offset", default=0, help="Number of candles to skip")
@click.option(
    "--sort-order", default="asc", type=click.Choice(["asc", "desc"]), help="Sort order"
)
@click.pass_context
def candles(
    ctx,
    pair: str,
    period: str,
    start: str | None,
    end: str | None,
    limit: int,
    offset: int,
    sort_order: str,
):
    """Get OHLCV candle data for a trading pair."""

    async def _candles():
        client = DataManagerClient(
            base_url=ctx.obj["base_url"], timeout=ctx.obj["timeout"]
        )

        try:
            start_dt = datetime.fromisoformat(start) if start else None
            end_dt = datetime.fromisoformat(end) if end else None

            result = await client.get_candles(
                pair=pair,
                period=period,
                start=start_dt,
                end=end_dt,
                limit=limit,
                offset=offset,
                sort_order=sort_order,
            )

            print(json.dumps(result, indent=2, default=str))

        finally:
            await client.close()

    asyncio.run(_candles())


@cli.command()
@click.argument("pair")
@click.option("--start", help="Start timestamp (ISO format)")
@click.option("--end", help="End timestamp (ISO format)")
@click.option("--limit", default=100, help="Maximum trades to return")
@click.option("--offset", default=0, help="Number of trades to skip")
@click.option(
    "--sort-order", default="asc", type=click.Choice(["asc", "desc"]), help="Sort order"
)
@click.pass_context
def trades(
    ctx,
    pair: str,
    start: str | None,
    end: str | None,
    limit: int,
    offset: int,
    sort_order: str,
):
    """Get individual trade data for a trading pair."""

    async def _trades():
        client = DataManagerClient(
            base_url=ctx.obj["base_url"], timeout=ctx.obj["timeout"]
        )

        try:
            start_dt = datetime.fromisoformat(start) if start else None
            end_dt = datetime.fromisoformat(end) if end else None

            result = await client.get_trades(
                pair=pair,
                start=start_dt,
                end=end_dt,
                limit=limit,
                offset=offset,
                sort_order=sort_order,
            )

            print(json.dumps(result, indent=2, default=str))

        finally:
            await client.close()

    asyncio.run(_trades())


@cli.command()
@click.argument("pair")
@click.pass_context
def depth(ctx, pair: str):
    """Get current order book depth for a trading pair."""

    async def _depth():
        client = DataManagerClient(
            base_url=ctx.obj["base_url"], timeout=ctx.obj["timeout"]
        )

        try:
            result = await client.get_depth(pair=pair)
            print(json.dumps(result, indent=2, default=str))

        finally:
            await client.close()

    asyncio.run(_depth())


@cli.command()
@click.pass_context
def health(ctx):
    """Check Data Manager API health status."""

    async def _health():
        client = DataManagerClient(
            base_url=ctx.obj["base_url"], timeout=ctx.obj["timeout"]
        )

        try:
            result = await client.health()
            print(json.dumps(result, indent=2, default=str))

        finally:
            await client.close()

    asyncio.run(_health())


def main():
    """Main entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
