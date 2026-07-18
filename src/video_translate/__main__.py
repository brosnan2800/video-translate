"""Enable `python -m video_translate`."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
