# Help illustrations

These original SVGs use the existing Forge cube faces, role colours and provider personas. Screen diagrams are labelled illustrations, not screenshots of live results. Each SVG has a title, description, fixed viewBox and light/dark palette; it contains no scripts, remote resources or embedded HTML.

To add an illustration, put its filename and intrinsic dimensions in `ui/shared/help-assets.ts`, then use `![Meaningful alt text](assets/name.svg)` in a Help guide. The Help renderer and `/_ui/help-assets/:name` route share that catalog. Other image URLs are discarded; ordinary issue Markdown still cannot render images.

`forge/host_update.py` includes the SVGs in the UI release hash and copies them into the release's `docs/assets` directory. Run the Help unit and browser tests after editing the catalog or a guide. Browser tests check decoding, external-image removal, responsive layout and both illustration colour schemes.
