"""Stortinget as a second source, in the same style as Bergen (issue #25).

api.py fetches from data.stortinget.no, innhenter.py stores the decided saker and
their votes, dokumenter.py turns them into the documents the writer cites
(innstilling, vedtak, votering), and the model steps reuse Bergen's through the
source hooks in kilde.py.
"""
