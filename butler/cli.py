import json
import logging

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def serve():
    """Run the daemon (Telegram bot + scheduler + energy poller)."""
    from . import bot

    from logging.handlers import RotatingFileHandler

    from .config import settings

    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    fh = RotatingFileHandler(settings().data_path / "butler.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(fh)
    for noisy in ("httpx", "apscheduler.executors.default", "apscheduler.scheduler", "google_genai.models"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    bot.run()


@app.command("do")
def do(words: list[str]):
    """Try a command locally without Telegram, e.g. `butler do geyser status` (schedules need the daemon)."""
    from . import commands

    typer.echo(commands.execute(" ".join(words)))


@app.command()
def discover(seconds: int = 6):
    """Scan the LAN for Tuya plugs (to fill plugs.json)."""
    import tinytuya

    typer.echo("Scanning for Tuya devices…")
    found = tinytuya.deviceScan(verbose=False, maxretry=seconds)
    for ip, d in found.items():
        typer.echo(f"  tuya  {ip}  id={d.get('gwId')}  version={d.get('version')}")

@app.command()
def check():
    """Talk to every configured device directly (no daemon) and print its status."""
    from . import devices

    for name, dev in devices.registry().items():
        try:
            typer.echo(json.dumps(dev.status().to_dict()))
        except devices.PlugError as e:
            typer.echo(f"{name}: ERROR {e}")

if __name__ == "__main__":
    app()
