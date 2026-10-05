#!/usr/bin/env python3
"""sql-doc-gen command line (also installed as the ``sql-doc-gen`` console script)."""
import sys

from sqldocgen.cli import main

if __name__ == "__main__":
    sys.exit(main())
