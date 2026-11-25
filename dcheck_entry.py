"""Entry point for the PyInstaller-built executable (dcheck.exe)."""
from dcheck.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
