# Vendored, not fetched

CodeMirror 5.65.16 — MIT, © Marijn Haverbeke and others.
<https://codemirror.net/5/LICENSE>

Copied in rather than loaded from a CDN because trance runs on machines with no
internet, and because a page that silently loses its editor when a CDN is
blocked is worse than one that never had it. Nothing here is modified; the
appearance is adjusted from `style.css` instead, so this directory can be
replaced wholesale by a newer release.

Only the modes for languages the agents actually write are included. Adding one
is a file here and a line in `index.html`.
