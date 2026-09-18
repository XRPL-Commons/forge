# Local Markdown assets

- Marked 18.0.13: https://github.com/markedjs/marked (MIT).
- DOMPurify 3.4.15: https://github.com/cure53/DOMPurify (Apache-2.0 OR MPL-2.0).

Copied from the exact npm versions pinned in package.json. Update the pinned
packages, copy lib/marked.umd.js and dist/purify.min.js here with their licenses,
and run the issues browser checks. Production serves these files locally;
operators do not depend on a CDN. Markdown is sanitized before rendering.
