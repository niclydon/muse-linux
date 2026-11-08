"""The GNOME client (GTK 4 + libadwaita). See app.py."""


def main(argv=None) -> int:
    from .app import main as run

    return run(argv)
