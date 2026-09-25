import typer

app = typer.Typer(help="LeadRadar AI CLI")


@app.command()
def info() -> None:
    """Show AI engine info."""
    typer.echo("LeadRadar AI CLI - LangGraph analysis engine")


if __name__ == "__main__":
    app()
