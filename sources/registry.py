"""
sources/registry.py - The list of counties currently wired in.

Adding a new "easy tier" county later is just: write sources/<county>.py
implementing CountySource, then add one line here.
"""
from sources.bexar import BexarSource
from sources.collin import CollinSource

ENABLED_SOURCES = [
    BexarSource(),
    CollinSource(),
]
