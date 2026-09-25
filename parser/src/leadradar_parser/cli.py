import typer

app = typer.Typer(help="LeadRadar Parser CLI")


@app.command()
def info() -> None:
    """Show parser library info."""
    typer.echo("LeadRadar Parser CLI - Data ingestion & normalization")


if __name__ == "__main__":
    app()
