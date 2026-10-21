from __future__ import absolute_import
import fire as fire
from perfcat import __version__
from perfcat.web import main

if __name__ == "__main__":
    fire.Fire(main)
