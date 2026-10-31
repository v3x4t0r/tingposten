"""Debatt: reader accounts and comments on each sak, moderated before they are shown.

The comment server runs on the Pi behind Tailscale Funnel; the static site loads
the comments with a small script (config.DEBATT = the server's address, None = off).
Every comment is judged against the rules in regler.py by the moderator chain in
moderering.py before anyone else sees it; what the model is unsure about waits for
the editor.
"""
