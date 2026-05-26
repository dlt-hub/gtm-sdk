import sys

import typer

from cli.accounts import app as accounts_app
from cli.apollo import app as apollo_app
from cli.attio import app as attio_app
from cli.enrichment import app as enrichment_app
from cli.gmail import app as gmail_app
from cli.granola import app as granola_app
from cli.parallel import app as parallel_app
from cli.webhook import app as webhook_app
from libs.logging.structured import set_source
from libs.telemetry import emit_cli_event, init_log_exporter, init_tracer

_CLI_SERVICE_NAME = "elvis-cli"

app = typer.Typer(
    name="gtm",
    help="GTM CLI",
    no_args_is_help=True,
)


def hello(name: str = typer.Argument("world", help="Name to greet")) -> None:
    """Say hello."""
    typer.echo(f"Hello, {name}!")


app.command()(hello)


def version() -> None:
    """Show the CLI version."""
    typer.echo("gtm v0.1.0")


app.command()(version)


app.add_typer(accounts_app, name="accounts")
app.add_typer(apollo_app, name="apollo")
app.add_typer(attio_app, name="attio")
app.add_typer(enrichment_app, name="enrichment")
app.add_typer(gmail_app, name="gmail")
app.add_typer(granola_app, name="granola")
app.add_typer(parallel_app, name="parallel")
app.add_typer(webhook_app, name="webhook")


def run():
    init_tracer(_CLI_SERVICE_NAME)
    # Bind the `source` contextvar so structured.log() calls in CLI flows
    # find the right OTLP logger via get_otlp_logger(source). The same
    # service name is passed to init_log_exporter so the lookup key
    # matches the registered service. (Strict lookup, no any-logger
    # fallback — see libs/telemetry.py:get_otlp_logger.)
    set_source(_CLI_SERVICE_NAME)
    init_log_exporter(_CLI_SERVICE_NAME)
    try:
        app()

    except SystemExit as exc:
        if exc.code == 2:
            emit_cli_event(
                "cli.usage_error",
                {
                    "raw_args": sys.argv[1:],
                    "exit_code": 2,
                },
            )
        raise


if __name__ == "__main__":
    run()
