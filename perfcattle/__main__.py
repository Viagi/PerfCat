from __future__ import absolute_import
import fire as fire
from perfcattle import __version__
from perfcattle.web import main

if __name__ == "__main__":
    fire.Fire(main)
