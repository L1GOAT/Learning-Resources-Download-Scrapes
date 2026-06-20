"""scrape.upload 包入口：python -m scrape.upload ..."""
from .cli import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
