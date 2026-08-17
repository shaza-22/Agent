"""Banque Misr data-gathering pipeline.

Stage 1 (discover + crawl) produces an immutable raw store of HTTP responses.
Stage 2 (extract) turns that raw store into a structured, citable corpus.
The two stages are separate so a parser bug never forces a re-crawl of the site.
"""
